/**
 * Inventory.jsx — Resource Inventory browser page.
 *
 * Phase 30: Shows all Azure resources fetched via Resource Graph.
 * - Summary cards (total resources, VMs, App Services, type count)
 * - Staleness warning + Refresh button
 * - Filterable + searchable resource table
 * - Row expand for full properties / tags
 * - VM power state colored dot
 */

import React, { useCallback, useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { RefreshCw, Server, ChevronDown, ChevronRight, AlertTriangle } from 'lucide-react'
import { refreshInventory, fetchRefreshStatus, fetchInventory, fetchInventoryStatus } from '../api'
import GlowCard from '../components/magicui/GlowCard'
import NumberTicker from '../components/magicui/NumberTicker'

// ── Helpers ────────────────────────────────────────────────────────────────

function formatAge(ageHours) {
  if (ageHours == null) return '—'
  if (ageHours < 1) return `${Math.round(ageHours * 60)}m ago`
  if (ageHours < 48) return `${Math.round(ageHours)}h ago`
  return `${Math.round(ageHours / 24)}d ago`
}

function shortType(type) {
  if (!type) return '—'
  const parts = type.split('/')
  return parts[parts.length - 1] || type
}

function PowerDot({ state }) {
  if (!state) return null
  const s = state.toLowerCase()
  const color = s.includes('running') ? '#10b981'
    : s.includes('dealloc') || s.includes('stopped') ? '#ef4444'
    : '#64748b'
  return (
    <span
      title={state}
      style={{
        display: 'inline-block',
        width: 8, height: 8, borderRadius: '50%',
        background: color,
        marginRight: 6,
        boxShadow: `0 0 6px ${color}88`,
        flexShrink: 0,
      }}
    />
  )
}

function ResourceRow({ resource, expanded, onToggle }) {
  const isVm = (resource.type || '').toLowerCase() === 'microsoft.compute/virtualmachines'
  const tags = resource.tags || {}
  const props = resource.properties || {}

  return (
    <>
      <tr
        onClick={onToggle}
        style={{
          cursor: 'pointer',
          borderBottom: '1px solid rgba(30,45,74,0.4)',
          background: expanded ? 'rgba(59,130,246,0.04)' : 'transparent',
        }}
        className="hover:bg-slate-800/30 transition-colors"
      >
        <td className="px-3 py-2.5 w-6">
          {expanded
            ? <ChevronDown className="w-3.5 h-3.5 text-slate-500" />
            : <ChevronRight className="w-3.5 h-3.5 text-slate-500" />}
        </td>
        <td className="px-2 py-2.5">
          <div className="flex items-center gap-1.5">
            {isVm && <PowerDot state={resource.powerState} />}
            <span className="text-sm text-slate-200 font-medium">{resource.name || '—'}</span>
          </div>
        </td>
        <td className="px-2 py-2.5 text-xs text-slate-400 font-mono">{shortType(resource.type)}</td>
        <td className="px-2 py-2.5 text-xs text-slate-500">{resource.resourceGroup || '—'}</td>
        <td className="px-2 py-2.5 text-xs text-slate-500">{resource.location || '—'}</td>
      </tr>
      {expanded && (
        <tr style={{ background: 'rgba(15,23,42,0.6)', borderBottom: '1px solid rgba(30,45,74,0.4)' }}>
          <td colSpan={5} className="px-6 py-3">
            <div className="grid grid-cols-1 gap-2 text-xs">
              <div>
                <span className="text-slate-500">ARM ID: </span>
                <span className="text-slate-300 font-mono break-all">{resource.id || '—'}</span>
              </div>
              {isVm && resource.powerState && (
                <div>
                  <span className="text-slate-500">Power State: </span>
                  <span className="text-slate-300">{resource.powerState}</span>
                </div>
              )}
              {resource.sku && (
                <div>
                  <span className="text-slate-500">SKU: </span>
                  <span className="text-slate-300">
                    {typeof resource.sku === 'string' ? resource.sku
                      : [resource.sku.name, resource.sku.tier, resource.sku.size].filter(Boolean).join(' / ')}
                  </span>
                </div>
              )}
              {Object.keys(props).length > 0 && (
                <div>
                  <span className="text-slate-500">Properties: </span>
                  <span className="text-slate-400">
                    {Object.entries(props)
                      .filter(([, v]) => typeof v !== 'object' && v !== null && v !== '')
                      .slice(0, 8)
                      .map(([k, v]) => `${k}=${String(v).slice(0, 60)}`)
                      .join(', ')}
                  </span>
                </div>
              )}
              {Object.keys(tags).length > 0 && (
                <div>
                  <span className="text-slate-500">Tags: </span>
                  <span className="text-slate-400">
                    {Object.entries(tags).slice(0, 10).map(([k, v]) => `${k}=${v}`).join(', ')}
                  </span>
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

// ── Main page ──────────────────────────────────────────────────────────────

export default function Inventory() {
  const { inventoryStatus: ctxStatus, fetchAll } = useOutletContext()

  const [inventory,       setInventory]       = useState(null)
  const [invStatus,       setInvStatus]       = useState(ctxStatus)
  const [refreshing,      setRefreshing]      = useState(false)
  const [refreshMsg,      setRefreshMsg]      = useState('')
  const [filter,          setFilter]          = useState('')
  const [typeFilter,      setTypeFilter]      = useState('')
  const [rgFilter,        setRgFilter]        = useState('')
  const [subFilter,       setSubFilter]       = useState('')
  const [expandedRows,    setExpandedRows]    = useState(new Set())
  const [loading,         setLoading]         = useState(true)

  // Load full inventory on mount
  useEffect(() => {
    async function load() {
      setLoading(true)
      try {
        const [inv, status] = await Promise.all([
          fetchInventory(),
          fetchInventoryStatus().catch(() => null),
        ])
        setInventory(inv)
        // Prefer the status response (includes backend-authoritative stale flag);
        // fall back to deriving metadata from the inventory response.
        if (status) {
          setInvStatus(status)
        } else if (inv) {
          setInvStatus({
            exists: true,
            refreshed_at: inv.refreshed_at,
            resource_count: inv.resource_count,
            type_summary: inv.type_summary,
          })
        }
      } catch {
        // no inventory yet
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [])

  // Keep status in sync with context updates
  useEffect(() => {
    if (ctxStatus) setInvStatus(ctxStatus)
  }, [ctxStatus])

  const handleRefresh = useCallback(async () => {
    setRefreshing(true)
    setRefreshMsg('Starting refresh…')
    try {
      const { refresh_id } = await refreshInventory()

      // Poll until complete
      let done = false
      while (!done) {
        await new Promise(r => setTimeout(r, 2000))
        const status = await fetchRefreshStatus(refresh_id)
        setRefreshMsg(status.message || status.status)
        if (status.status === 'complete') {
          done = true
          // Reload full inventory
          const inv = await fetchInventory()
          setInventory(inv)
          if (inv) setInvStatus({ exists: true, refreshed_at: inv.refreshed_at, resource_count: inv.resource_count, type_summary: inv.type_summary })
          fetchAll().catch(() => {})
        } else if (status.status === 'failed') {
          done = true
          setRefreshMsg(`Failed: ${status.error || 'unknown error'}`)
        }
      }
    } catch (err) {
      setRefreshMsg(`Error: ${err.message}`)
    } finally {
      setRefreshing(false)
      setTimeout(() => setRefreshMsg(''), 3000)
    }
  }, [fetchAll])

  // Derived data
  const resources = inventory?.resources || []
  const allTypes = [...new Set(resources.map(r => (r.type || '').toLowerCase()))].sort()
  const allRGs = [...new Set(resources.map(r => r.resourceGroup || '').filter(Boolean))].sort()

  // Extract subscription IDs from ARM resource IDs (/subscriptions/{id}/...)
  function extractSubId(id) {
    if (!id) return null
    const m = id.match(/\/subscriptions\/([^/]+)\//i)
    return m ? m[1].toLowerCase() : null
  }
  const allSubs = [...new Set(resources.map(r => extractSubId(r.id)).filter(Boolean))].sort()

  const filtered = resources.filter(r => {
    const matchType = !typeFilter || (r.type || '').toLowerCase() === typeFilter
    const matchRg   = !rgFilter   || (r.resourceGroup || '').toLowerCase() === rgFilter.toLowerCase()
    const matchSub  = !subFilter  || extractSubId(r.id) === subFilter.toLowerCase()
    const matchSearch = !filter
      || (r.name || '').toLowerCase().includes(filter.toLowerCase())
      || (r.resourceGroup || '').toLowerCase().includes(filter.toLowerCase())
    return matchType && matchRg && matchSub && matchSearch
  })

  const vmCount = resources.filter(r => (r.type || '').toLowerCase() === 'microsoft.compute/virtualmachines').length
  const appSvcCount = resources.filter(r => (r.type || '').toLowerCase() === 'microsoft.web/sites').length
  const typeCount = allTypes.length

  const stale = invStatus?.stale ?? true
  const ageHours = invStatus?.age_hours

  function toggleRow(id) {
    setExpandedRows(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Resource Inventory</h1>
          {invStatus?.exists && (
            <p className="text-xs text-slate-500 mt-0.5">
              Last refreshed: {formatAge(ageHours)}
              {invStatus.refreshed_at && ` (${new Date(invStatus.refreshed_at).toLocaleString()})`}
            </p>
          )}
        </div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
          style={{
            background: refreshing ? 'rgba(59,130,246,0.1)' : 'rgba(59,130,246,0.15)',
            border: '1px solid rgba(59,130,246,0.3)',
            color: refreshing ? '#64748b' : '#93c5fd',
          }}
        >
          <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
          {refreshing ? (refreshMsg || 'Refreshing…') : 'Refresh Inventory'}
        </button>
      </div>

      {/* Staleness warning */}
      {stale && invStatus?.exists && (
        <div
          className="flex items-center gap-3 px-4 py-3 rounded-lg text-sm"
          style={{
            background: 'rgba(245,158,11,0.08)',
            border: '1px solid rgba(245,158,11,0.25)',
            color: '#fcd34d',
          }}
        >
          <AlertTriangle className="w-4 h-4 shrink-0" />
          <span>
            Inventory is {ageHours ? `${Math.round(ageHours)}h` : ''} old — consider refreshing for accurate scan results.
          </span>
        </div>
      )}

      {!invStatus?.exists && !loading && (
        <div
          className="flex items-center gap-3 px-4 py-3 rounded-lg text-sm"
          style={{
            background: 'rgba(100,116,139,0.08)',
            border: '1px solid rgba(100,116,139,0.2)',
            color: '#94a3b8',
          }}
        >
          <Server className="w-4 h-4 shrink-0" />
          <span>No inventory found. Click <strong>Refresh Inventory</strong> to fetch all Azure resources.</span>
        </div>
      )}

      {/* Summary cards — only render derived stats once inventory is loaded to avoid 0→N ticker flicker */}
      {invStatus?.exists && (
        loading ? (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            {[...Array(4)].map((_, i) => (
              <GlowCard key={i} glowColor="rgba(59,130,246,0.15)" className="p-4">
                <div className="h-3 w-24 bg-slate-700/60 rounded mb-2 animate-pulse" />
                <div className="h-8 w-12 bg-slate-700/40 rounded animate-pulse" />
              </GlowCard>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <GlowCard glowColor="rgba(59,130,246,0.3)" className="p-4">
              <div className="text-xs text-slate-500 mb-1">Total Resources</div>
              <div className="text-2xl font-bold text-slate-100">
                <NumberTicker value={invStatus.resource_count || 0} />
              </div>
            </GlowCard>
            <GlowCard glowColor="rgba(16,185,129,0.3)" className="p-4">
              <div className="text-xs text-slate-500 mb-1">Virtual Machines</div>
              <div className="text-2xl font-bold text-slate-100">
                <NumberTicker value={vmCount} />
              </div>
            </GlowCard>
            <GlowCard glowColor="rgba(139,92,246,0.3)" className="p-4">
              <div className="text-xs text-slate-500 mb-1">App Services</div>
              <div className="text-2xl font-bold text-slate-100">
                <NumberTicker value={appSvcCount} />
              </div>
            </GlowCard>
            <GlowCard glowColor="rgba(245,158,11,0.3)" className="p-4">
              <div className="text-xs text-slate-500 mb-1">Resource Types</div>
              <div className="text-2xl font-bold text-slate-100">
                <NumberTicker value={typeCount} />
              </div>
            </GlowCard>
          </div>
        )
      )}

      {/* Filter row — only render after load to avoid count flicker (BUG-005) */}
      {resources.length > 0 && !loading && (
        <div className="flex gap-3 flex-wrap">
          <select
            value={typeFilter}
            onChange={e => setTypeFilter(e.target.value)}
            className="px-3 py-2 rounded-lg text-sm bg-slate-800/60 border border-slate-700 text-slate-300 focus:outline-none focus:border-blue-500/50"
          >
            <option value="">All types ({allTypes.length})</option>
            {allTypes.map(t => (
              <option key={t} value={t}>{shortType(t)} ({(invStatus?.type_summary?.[t] || resources.filter(r => (r.type || '').toLowerCase() === t).length)})</option>
            ))}
          </select>
          <select
            value={rgFilter}
            onChange={e => setRgFilter(e.target.value)}
            className="px-3 py-2 rounded-lg text-sm bg-slate-800/60 border border-slate-700 text-slate-300 focus:outline-none focus:border-blue-500/50"
          >
            <option value="">All resource groups ({allRGs.length})</option>
            {allRGs.map(rg => (
              <option key={rg} value={rg}>{rg}</option>
            ))}
          </select>
          {allSubs.length > 1 && (
            <select
              value={subFilter}
              onChange={e => setSubFilter(e.target.value)}
              className="px-3 py-2 rounded-lg text-sm bg-slate-800/60 border border-slate-700 text-slate-300 focus:outline-none focus:border-blue-500/50"
            >
              <option value="">All subscriptions ({allSubs.length})</option>
              {allSubs.map(sub => (
                <option key={sub} value={sub}>{sub}</option>
              ))}
            </select>
          )}
          <input
            type="text"
            placeholder="Search by name or resource group…"
            value={filter}
            onChange={e => setFilter(e.target.value)}
            className="flex-1 min-w-[200px] px-3 py-2 rounded-lg text-sm bg-slate-800/60 border border-slate-700 text-slate-300 placeholder-slate-600 focus:outline-none focus:border-blue-500/50"
          />
          <div className="text-sm text-slate-500 self-center">
            {filtered.length} / {resources.length} resources
          </div>
        </div>
      )}

      {/* Resource table */}
      {loading ? (
        <div className="flex items-center justify-center py-16">
          <div className="w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
        </div>
      ) : resources.length > 0 ? (
        <div
          className="rounded-xl overflow-hidden"
          style={{ background: 'rgba(5,15,35,0.6)', border: '1px solid rgba(30,45,74,0.5)' }}
        >
          <table className="w-full text-left">
            <thead>
              <tr style={{ borderBottom: '1px solid rgba(30,45,74,0.6)', background: 'rgba(15,23,42,0.5)' }}>
                <th className="px-3 py-2 w-6" />
                <th className="px-2 py-2 text-xs font-semibold text-slate-400 uppercase tracking-wider">Name</th>
                <th className="px-2 py-2 text-xs font-semibold text-slate-400 uppercase tracking-wider">Type</th>
                <th className="px-2 py-2 text-xs font-semibold text-slate-400 uppercase tracking-wider">Resource Group</th>
                <th className="px-2 py-2 text-xs font-semibold text-slate-400 uppercase tracking-wider">Location</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r, i) => {
                const key = r.id || `${r.name}-${i}`
                return (
                  <ResourceRow
                    key={key}
                    resource={r}
                    expanded={expandedRows.has(key)}
                    onToggle={() => toggleRow(key)}
                  />
                )
              })}
            </tbody>
          </table>
          {filtered.length === 0 && (
            <div className="py-8 text-center text-sm text-slate-500">
              No resources match your filters.
            </div>
          )}
        </div>
      ) : null}
    </div>
  )
}
