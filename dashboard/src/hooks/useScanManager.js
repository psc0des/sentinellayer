/**
 * useScanManager.js — single source of truth for ALL scan state.
 *
 * Refresh resilience strategy (two-layer):
 *   1. fetchAgentLastRun() — fast, per-agent, may use in-memory backend state
 *   2. fetchScanHistory()  — Cosmos-backed fallback; survives backend restarts
 *      and container revision swaps. Used when layer 1 doesn't return running.
 *
 * Stopping resilience:
 *   When Stop is clicked, the scan_id is written to localStorage under
 *   'ruriskry-stopping-scans'. On page refresh, if the backend still reports
 *   the scan as 'running' AND it's in that localStorage set, we restore it as
 *   'stopping' and re-send the cancel request. The set is cleaned up as soon as
 *   the backend confirms a terminal state.
 */

import { useState, useRef, useCallback, useEffect } from 'react'
import {
  triggerScan,
  triggerAllScans,
  cancelScan,
  fetchScanStatus,
  fetchAgentLastRun,
  fetchScanHistory,
} from '../api'

// ── Constants ────────────────────────────────────────────────────────────────

const AGENT_TYPES = ['cost', 'monitoring', 'deploy']

const AGENT_NAME = {
  cost:       'cost-optimization-agent',
  monitoring: 'monitoring-agent',
  deploy:     'deploy-agent',
}

const LS_STOPPING_KEY = 'ruriskry-stopping-scans'

// ── localStorage helpers ─────────────────────────────────────────────────────

function getStoppingSet() {
  try {
    return new Set(JSON.parse(localStorage.getItem(LS_STOPPING_KEY) ?? '[]'))
  } catch { return new Set() }
}

function addStopping(scanId) {
  if (!scanId) return
  try {
    const s = getStoppingSet()
    s.add(scanId)
    localStorage.setItem(LS_STOPPING_KEY, JSON.stringify([...s]))
  } catch { /* ignore storage errors */ }
}

function removeStopping(scanId) {
  if (!scanId) return
  try {
    const s = getStoppingSet()
    s.delete(scanId)
    localStorage.setItem(LS_STOPPING_KEY, JSON.stringify([...s]))
  } catch { /* ignore storage errors */ }
}

// ── Normalize API agent_type values → our internal types ─────────────────────

function normalizeType(t) {
  if (!t) return null
  if (t === 'cost-optimization') return 'cost'
  return t
}

function emptyAgentState() {
  return { status: 'idle', scanId: null, startedAt: null }
}

function initialScanState() {
  return {
    cost:       emptyAgentState(),
    monitoring: emptyAgentState(),
    deploy:     emptyAgentState(),
  }
}

// ── Hook ─────────────────────────────────────────────────────────────────────

export default function useScanManager({ onScanComplete } = {}) {
  const [scanState, setScanState] = useState(initialScanState)
  const [logViewer, setLogViewer] = useState({
    open: false,
    scanId: null,
    agentType: null,
    mode: null,
    scanEntries: null,
  })

  const [resourceGroup,  setResourceGroup]  = useState('')
  const [subscriptionId, setSubscriptionId] = useState('')

  const pollRefs = useRef({ cost: null, monitoring: null, deploy: null })

  const onScanCompleteRef = useRef(onScanComplete)
  useEffect(() => { onScanCompleteRef.current = onScanComplete }, [onScanComplete])

  const restoredRef = useRef(false)

  // ── Polling ──────────────────────────────────────────────────────────────

  const startPolling = useCallback((scanId, agentType) => {
    if (pollRefs.current[agentType]) clearInterval(pollRefs.current[agentType])

    pollRefs.current[agentType] = setInterval(async () => {
      try {
        const result = await fetchScanStatus(scanId)
        if (result.status !== 'running') {
          clearInterval(pollRefs.current[agentType])
          pollRefs.current[agentType] = null
          // Clean up localStorage stopping set when scan reaches terminal state
          removeStopping(scanId)
          let resolvedStatus
          if (result.status === 'error') resolvedStatus = 'error'
          else if (result.status === 'cancelled') resolvedStatus = 'cancelled'
          else resolvedStatus = 'complete'
          setScanState(prev => ({
            ...prev,
            [agentType]: { status: resolvedStatus, scanId, startedAt: null },
          }))
          onScanCompleteRef.current?.()
        }
      } catch {
        // Network hiccup — keep polling
      }
    }, 3_000)
  }, [])

  // ── Mount: two-layer restore ──────────────────────────────────────────────

  useEffect(() => {
    if (restoredRef.current) return
    restoredRef.current = true

    const stoppingSet = getStoppingSet()

    const restoreAgent = (agentType, scanId, startedAt) => {
      // If this scan_id was in localStorage stopping set, it means the user
      // clicked Stop before the last page refresh. Restore as 'stopping' and
      // re-send the cancel request so the backend processes it.
      const wasStopping = stoppingSet.has(scanId)
      setScanState(prev => ({
        ...prev,
        [agentType]: {
          status: wasStopping ? 'stopping' : 'running',
          scanId,
          startedAt: startedAt ?? new Date().toISOString(),
        },
      }))
      startPolling(scanId, agentType)
      if (wasStopping) {
        // Re-send cancel — backend will ignore it if the scan is already done
        cancelScan(scanId).catch(() => { /* scan may already be terminal */ })
      }
    }

    // Layer 1: fetchAgentLastRun (fast, per-agent)
    Promise.allSettled(AGENT_TYPES.map(t => fetchAgentLastRun(AGENT_NAME[t])))
      .then(results => {
        const restoredTypes = new Set()

        results.forEach((r, i) => {
          if (r.status === 'fulfilled' && r.value?.status === 'running' && r.value?.scan_id) {
            const agentType = AGENT_TYPES[i]
            restoreAgent(agentType, r.value.scan_id, r.value.started_at)
            restoredTypes.add(agentType)
          }
        })

        // Layer 2: fetchScanHistory (Cosmos-backed, survives restarts)
        // Only needed if at least one agent wasn't restored by layer 1
        if (restoredTypes.size < AGENT_TYPES.length) {
          fetchScanHistory(50)
            .then(data => {
              const scans = data?.scans ?? data ?? []
              // Find the most recent running scan per agent type
              scans.forEach(scan => {
                if (scan.status !== 'running' || !scan.scan_id) return
                const agentType = normalizeType(scan.agent_type)
                if (!agentType || restoredTypes.has(agentType)) return
                restoreAgent(agentType, scan.scan_id, scan.started_at)
                restoredTypes.add(agentType)
              })
            })
            .catch(() => {/* ignore — best effort */})
        }
      })
  }, [startPolling])

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      AGENT_TYPES.forEach(t => {
        if (pollRefs.current[t]) clearInterval(pollRefs.current[t])
      })
    }
  }, [])

  // ── Actions ──────────────────────────────────────────────────────────────

  const startScan = useCallback(async (agentType, inventoryMode = 'existing') => {
    const rg = resourceGroup.trim() || null
    const sub = subscriptionId.trim() || null
    const now = new Date().toISOString()
    setScanState(prev => ({
      ...prev,
      [agentType]: { status: 'running', scanId: null, startedAt: now },
    }))
    try {
      const { scan_id } = await triggerScan(agentType, rg, sub, inventoryMode)
      setScanState(prev => ({
        ...prev,
        [agentType]: { status: 'running', scanId: scan_id, startedAt: now },
      }))
      setLogViewer({ open: true, scanId: scan_id, agentType, mode: 'live', scanEntries: null })
      startPolling(scan_id, agentType)
    } catch (err) {
      setScanState(prev => ({
        ...prev,
        [agentType]: { status: 'error', scanId: null, startedAt: null, error: err.message },
      }))
    }
  }, [resourceGroup, subscriptionId, startPolling])

  const startAllScans = useCallback(async (inventoryMode = 'existing') => {
    const rg = resourceGroup.trim() || null
    const sub = subscriptionId.trim() || null
    const now = new Date().toISOString()
    setScanState({
      cost:       { status: 'running', scanId: null, startedAt: now },
      monitoring: { status: 'running', scanId: null, startedAt: now },
      deploy:     { status: 'running', scanId: null, startedAt: now },
    })
    try {
      const { scan_ids } = await triggerAllScans(rg, sub, inventoryMode)
      const entries = []
      AGENT_TYPES.forEach((t, i) => {
        const scanId = scan_ids[i]
        if (scanId) {
          setScanState(prev => ({
            ...prev,
            [t]: { status: 'running', scanId, startedAt: now },
          }))
          startPolling(scanId, t)
          entries.push({ scanId, agentType: t })
        }
      })
      setLogViewer({ open: true, scanId: null, agentType: 'all', mode: 'live', scanEntries: entries })
    } catch (err) {
      const errState = { status: 'error', scanId: null, startedAt: null, error: err.message }
      setScanState({ cost: errState, monitoring: errState, deploy: errState })
    }
  }, [resourceGroup, subscriptionId, startPolling])

  const stopScan = useCallback(async (agentType) => {
    const scanId = scanState[agentType]?.scanId
    // Immediately show "stopping" — keep polling so we catch the real terminal state.
    // Persist to localStorage so page refresh also restores as 'stopping'.
    setScanState(prev => ({
      ...prev,
      [agentType]: { ...prev[agentType], status: 'stopping' },
    }))
    if (scanId) {
      addStopping(scanId)
      try { await cancelScan(scanId) } catch { /* may already be complete */ }
      // Keep the poll running; if it was never started (edge case), start it now.
      if (!pollRefs.current[agentType]) {
        startPolling(scanId, agentType)
      }
    } else {
      // No scan_id yet — nothing to cancel, clear immediately.
      setScanState(prev => ({
        ...prev,
        [agentType]: { status: 'cancelled', scanId: null, startedAt: null },
      }))
    }
  }, [scanState, startPolling])

  const stopAllScans = useCallback(async () => {
    await Promise.allSettled(
      AGENT_TYPES
        .filter(t => scanState[t]?.status === 'running')
        .map(t => stopScan(t))
    )
  }, [scanState, stopScan])

  // ── Log viewer controls ──────────────────────────────────────────────────

  const openLiveLog = useCallback((agentType) => {
    if (agentType === 'all') {
      const entries = AGENT_TYPES
        .filter(t => scanState[t]?.scanId && scanState[t]?.status === 'running')
        .map(t => ({ scanId: scanState[t].scanId, agentType: t }))
      setLogViewer({ open: true, scanId: null, agentType: 'all', mode: 'live', scanEntries: entries })
    } else {
      const scanId = scanState[agentType]?.scanId
      if (scanId) {
        setLogViewer({ open: true, scanId, agentType, mode: 'live', scanEntries: null })
      }
    }
  }, [scanState])

  const openHistoricalLog = useCallback((scanId, agentType) => {
    setLogViewer({ open: true, scanId, agentType, mode: 'historical', scanEntries: null })
  }, [])

  const closeLogs = useCallback(() => {
    setLogViewer(prev => ({ ...prev, open: false }))
  }, [])

  // ── Derived ──────────────────────────────────────────────────────────────

  const anyScanning = AGENT_TYPES.some(t => ['running', 'stopping'].includes(scanState[t]?.status))
  const allScanning = AGENT_TYPES.every(t => scanState[t]?.status === 'running')
  const anyStopping = AGENT_TYPES.some(t => scanState[t]?.status === 'stopping')

  return {
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
    AGENT_TYPES,
    AGENT_NAME,
  }
}
