/**
 * Overview.jsx — landing page of the RuriSkry dashboard.
 *
 * Phase 24 redesign: Magic UI aesthetic
 *   - NumberTicker on all numeric metrics (count-up animation)
 *   - GlowCard wrappers with color-coded glow
 *   - Gradient AreaChart for SRI trend
 *   - Border beam on active/alert cards
 *   - Staggered entrance animations
 */

import React, { useEffect, useState } from 'react'
import { useOutletContext, useNavigate, NavLink } from 'react-router-dom'
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid,
} from 'recharts'
import {
  CheckCircle, AlertTriangle, Clock, Zap, TrendingUp, RefreshCw, Cpu,
  Activity as ActivityIcon, Terminal,
} from 'lucide-react'
import { fetchAgentLastRun } from '../api'
import NumberTicker from '../components/magicui/NumberTicker'
import GlowCard from '../components/magicui/GlowCard'
import VerdictBadge from '../components/magicui/VerdictBadge'
import TableSkeleton from '../components/magicui/TableSkeleton'

// ── Helpers ────────────────────────────────────────────────────────────────

function formatTime(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch { return iso }
}

function relativeTime(iso) {
  if (!iso) return '—'
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 10) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  return `${Math.floor(m / 60)}h ago`
}

function scanDuration(started, completed) {
  if (!started || !completed) return null
  const s = Math.round((new Date(completed) - new Date(started)) / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}

const AGENT_NAMES = ['cost-optimization-agent', 'monitoring-agent', 'deploy-agent']
const AGENT_LABELS = {
  'cost-optimization-agent': 'Cost',
  'monitoring-agent':        'Monitoring',
  'deploy-agent':            'Deploy',
}
const AGENT_TYPE_LABELS = {
  cost:       'Cost',
  monitoring: 'Monitoring',
  deploy:     'Deploy',
  sre:        'Monitoring',
}

// ── Custom tooltip for SRI chart ───────────────────────────────────────────

function SRITooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-slate-900/95 border border-slate-700 rounded-lg px-3 py-2 shadow-xl backdrop-blur">
      <p className="text-[11px] text-slate-400 mb-1">{label}</p>
      <p className="text-sm font-bold text-blue-300">
        SRI <span className="text-white">{Number(payload[0].value).toFixed(1)}</span>
      </p>
    </div>
  )
}

// ── MetricCard ─────────────────────────────────────────────────────────────

function MetricCard({ label, numericValue, displayValue, decimals, suffix, sub, icon: Icon, color, urgent = false }) {
  const iconAccent = {
    blue:   'bg-blue-500/10 text-blue-400',
    green:  'bg-emerald-500/10 text-emerald-400',
    amber:  'bg-amber-500/10 text-amber-400',
    red:    'bg-rose-500/10 text-rose-400',
    slate:  'bg-slate-800 text-slate-500',
  }
  const valueGlowClass = {
    blue:  'metric-value metric-value-blue',
    green: 'metric-value metric-value-green',
    amber: 'metric-value metric-value-amber',
    red:   'metric-value metric-value-red',
    slate: 'metric-value metric-value-slate',
  }

  const useNumericDisplay = numericValue !== undefined && numericValue !== null && !isNaN(Number(numericValue))

  return (
    <GlowCard color={color} intensity="medium" beam urgent={urgent} className="p-5 flex items-start gap-4">
      <div className={`w-10 h-10 rounded-lg flex items-center justify-center shrink-0 ${iconAccent[color] ?? iconAccent.blue}`}>
        <Icon className="w-5 h-5" />
      </div>
      <div className="min-w-0">
        <p className={valueGlowClass[color] ?? valueGlowClass.slate}>
          {useNumericDisplay
            ? <NumberTicker value={Number(numericValue)} decimals={decimals ?? 0} suffix={suffix ?? ''} />
            : displayValue ?? '—'
          }
        </p>
        <p className="text-sm text-slate-400 font-medium mt-1.5">{label}</p>
        {sub && <p className="text-xs text-slate-600 mt-0.5">{sub}</p>}
      </div>
    </GlowCard>
  )
}

// ── PendingCard ────────────────────────────────────────────────────────────

function PendingCard({ review, onNavigate }) {
  const ev = review.verdict_snapshot ?? {}
  const resourceName = ev.resource_id?.split('/').filter(Boolean).pop() ?? ev.resource_id ?? '—'
  const isEscalated = review.status === 'awaiting_review'

  return (
    <GlowCard
      color={isEscalated ? 'amber' : 'amber'}
      intensity="low"
      beam
      beamDuration={4}
      className="p-4 cursor-pointer hover:scale-[1.01] transition-transform"
      as="button"
      onClick={() => onNavigate(`/decisions?exec=${review.execution_id}`)}
    >
      <div className="flex items-start justify-between gap-3 mb-2">
        <span className="text-sm font-medium text-slate-200 font-mono truncate text-left" title={ev.resource_id}>
          {resourceName}
        </span>
        <span className={`shrink-0 text-[11px] px-2 py-0.5 rounded-full font-bold uppercase border ${
          isEscalated
            ? 'bg-amber-500/15 text-amber-300 border-amber-500/30'
            : 'bg-orange-500/15 text-orange-300 border-orange-500/30'
        }`}>
          {isEscalated ? 'Escalated' : 'Pending'}
        </span>
      </div>
      <p className="text-xs text-slate-500 text-left">
        {ev.action_type?.replace(/_/g, ' ') ?? '—'}
        {ev.sri_composite != null && ` · SRI ${ev.sri_composite.toFixed(1)}`}
      </p>
      <p className="text-xs text-slate-600 mt-1 text-left">
        {relativeTime(review.created_at ?? ev.timestamp)}
      </p>
    </GlowCard>
  )
}

// ── AlertsCard ─────────────────────────────────────────────────────────────

function AlertsCard({ total, active, resolved, resolutionRate }) {
  return (
    <GlowCard
      color={active > 0 ? 'red' : 'slate'}
      intensity={active > 0 ? 'medium' : 'low'}
      beam={active > 0}
      beamDuration={3}
      className="p-5"
    >
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-slate-300 flex items-center gap-2">
          <Zap className="w-4 h-4 text-slate-500" />
          Alert Activity
        </h2>
        <NavLink to="/alerts" className="text-xs text-blue-400 hover:text-blue-300 transition-colors">
          View all →
        </NavLink>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <p className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold mb-1">Total</p>
          <p className="text-2xl font-bold text-slate-200 tabular-nums" style={{ fontFamily: 'var(--font-data)' }}>
            <NumberTicker value={total} />
          </p>
        </div>
        <div>
          <p className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold mb-1">Active</p>
          <div className="flex items-center gap-2">
            <p className={`text-2xl font-bold tabular-nums ${active > 0 ? 'text-rose-400' : 'text-slate-500'}`} style={{ fontFamily: 'var(--font-data)' }}>
              <NumberTicker value={active} />
            </p>
            {active > 0 && (
              <span className="w-2 h-2 rounded-full bg-rose-500 animate-pulse" />
            )}
          </div>
        </div>
        <div>
          <p className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold mb-1">Investigated</p>
          <p className="text-2xl font-bold text-emerald-400 tabular-nums" style={{ fontFamily: 'var(--font-data)' }}>
            <NumberTicker value={resolved} />
          </p>
        </div>
        <div>
          <p className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold mb-1">Investigation Rate</p>
          <p className="text-2xl font-bold text-slate-300 tabular-nums" style={{ fontFamily: 'var(--font-data)' }}>
            {resolutionRate !== null ? <><NumberTicker value={resolutionRate} />%</> : <span className="text-slate-600">—</span>}
          </p>
        </div>
      </div>
    </GlowCard>
  )
}

// ── ExecutionMetricsCard ───────────────────────────────────────────────────

function ExecutionMetricsCard({ executions }) {
  if (!executions) return null
  return (
    <GlowCard color="purple" intensity="low" className="p-5">
      <div className="flex items-center gap-2 mb-4">
        <Terminal className="w-4 h-4 text-violet-400" />
        <h2 className="text-sm font-semibold text-slate-300">Execution Metrics</h2>
      </div>
      <div className="grid grid-cols-3 gap-3 mb-4">
        <div>
          <p className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold mb-1">Applied</p>
          <p className="text-2xl font-bold text-emerald-400 tabular-nums" style={{ fontFamily: 'var(--font-data)' }}>
            <NumberTicker value={executions.applied ?? 0} />
          </p>
        </div>
        <div>
          <p className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold mb-1">PR Created</p>
          <p className="text-2xl font-bold text-blue-400 tabular-nums" style={{ fontFamily: 'var(--font-data)' }}>
            <NumberTicker value={executions.pr_created ?? 0} />
          </p>
        </div>
        <div>
          <p className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold mb-1">Failed</p>
          <p className="text-2xl font-bold text-rose-400 tabular-nums" style={{ fontFamily: 'var(--font-data)' }}>
            <NumberTicker value={executions.failed ?? 0} />
          </p>
        </div>
      </div>
      <div className="border-t border-slate-800/60 pt-3 grid grid-cols-2 gap-3">
        <div>
          <p className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold mb-1">Agent Fix Rate</p>
          <p className="text-xl font-bold text-violet-400 tabular-nums" style={{ fontFamily: 'var(--font-data)' }}>
            <NumberTicker value={executions.agent_fix_rate ?? 0} decimals={1} suffix="%" />
          </p>
        </div>
        <div>
          <p className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold mb-1">Success Rate</p>
          <p className="text-xl font-bold text-emerald-400 tabular-nums" style={{ fontFamily: 'var(--font-data)' }}>
            <NumberTicker value={executions.success_rate ?? 0} decimals={1} suffix="%" />
          </p>
        </div>
      </div>
    </GlowCard>
  )
}

// ── Main Component ─────────────────────────────────────────────────────────

export default function Overview() {
  const { evaluations, metrics, agents, pendingReviews, alerts } = useOutletContext()
  const navigate = useNavigate()

  const [recentScans, setRecentScans] = useState([])
  const [scansLoading, setScansLoading] = useState(true)

  function loadScans() {
    setScansLoading(true)
    Promise.all(AGENT_NAMES.map(name => fetchAgentLastRun(name).catch(() => null)))
      .then(results => {
        setRecentScans(
          results
            .filter(r => r && r.status !== 'no_data')
            .sort((a, b) => (b.started_at ?? '').localeCompare(a.started_at ?? ''))
        )
        setScansLoading(false)
      })
  }

  useEffect(() => { loadScans() }, [])

  const lastScanTime = recentScans.reduce((latest, scan) => {
    const t = scan.completed_at ?? scan.started_at
    return (!latest || t > latest) ? t : latest
  }, null)

  // SRI trend — last 20 evaluations, oldest first
  const trendData = [...evaluations].reverse().slice(-20).map((ev, i) => ({
    i,
    sri: ev.sri_composite ?? 0,
    label: new Date(ev.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
  }))

  const approvedCount = metrics?.decisions?.approved ?? metrics?.approved ?? 0
  const approvalRate = metrics?.total_evaluations > 0
    ? Math.round((approvedCount / metrics.total_evaluations) * 100)
    : null
  const avgSri = metrics?.sri_composite?.avg ?? metrics?.avg_sri ?? 0

  const sriColor = avgSri <= 25 ? 'green' : avgSri <= 60 ? 'amber' : 'red'

  const alertTotal          = alerts?.length ?? 0
  const alertActive         = (alerts ?? []).filter(a => a.status === 'firing' || a.status === 'investigating').length
  const alertResolved       = (alerts ?? []).filter(a => a.status === 'resolved').length
  const alertResolutionRate = alertTotal > 0 ? Math.round(alertResolved / alertTotal * 100) : null

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">

      {/* ── Status banner ── */}
      <GlowCard
        color={pendingReviews.length > 0 ? 'amber' : 'blue'}
        intensity="low"
        className="px-5 py-3.5 flex flex-wrap items-center gap-x-5 gap-y-2 animate-fade-in-up"
      >
        <div className="flex items-center gap-2 text-sm">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-60" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
          </span>
          <span className="text-slate-200 font-medium">
            {agents.length} agent{agents.length !== 1 ? 's' : ''} connected
          </span>
        </div>

        <div className="w-px h-4 bg-slate-700 hidden sm:block" />

        <div className="flex items-center gap-1.5 text-sm text-slate-400">
          <Clock className="w-3.5 h-3.5 shrink-0 text-slate-500" />
          Last scan:{' '}
          <span className="text-slate-300 ml-1">{lastScanTime ? relativeTime(lastScanTime) : 'never'}</span>
        </div>

        {pendingReviews.length > 0 && (
          <>
            <div className="w-px h-4 bg-slate-700 hidden sm:block" />
            <div className="flex items-center gap-1.5 text-sm text-amber-400">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
              <button
                onClick={() => navigate('/decisions')}
                className="hover:text-amber-300 underline decoration-dotted underline-offset-2"
              >
                {pendingReviews.length} pending review{pendingReviews.length !== 1 ? 's' : ''}
              </button>
            </div>
          </>
        )}

        <div className="ml-auto text-xs text-slate-700 font-mono">Auto-refresh 5s</div>
      </GlowCard>

      {/* ── Metric cards ── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 card-stagger">
        <MetricCard
          label="Total evaluations"
          numericValue={metrics?.total_evaluations ?? evaluations.length}
          sub="all time"
          icon={Zap}
          color="blue"
        />
        <MetricCard
          label="Approval rate"
          numericValue={approvalRate}
          decimals={0}
          suffix="%"
          displayValue={approvalRate === null ? '—' : undefined}
          sub={`${approvedCount} approved`}
          icon={CheckCircle}
          color="green"
        />
        <MetricCard
          label="Avg SRI score"
          numericValue={avgSri}
          decimals={1}
          sub="composite risk index"
          icon={TrendingUp}
          color={sriColor}
        />
        <MetricCard
          label="Pending reviews"
          numericValue={pendingReviews.length}
          sub={pendingReviews.length === 0 ? 'all clear' : 'require action'}
          icon={AlertTriangle}
          color={pendingReviews.length > 0 ? 'amber' : 'slate'}
          urgent={pendingReviews.length > 0}
        />
      </div>

      {/* ── Alerts + Execution metrics ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <AlertsCard
          total={alertTotal}
          active={alertActive}
          resolved={alertResolved}
          resolutionRate={alertResolutionRate}
        />
        <ExecutionMetricsCard executions={metrics?.executions} />
      </div>

      {/* ── SRI trend + pending reviews ── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

        {/* SRI trend chart */}
        <GlowCard color="blue" intensity="low" className="lg:col-span-2 p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold text-slate-300">
              SRI trend
              <span className="ml-2 text-xs text-slate-500 font-normal">last 20 evaluations</span>
            </h2>
            {trendData.length > 0 && (
              <div className="flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full bg-blue-500" />
                <span className="text-xs text-slate-500">SRI score</span>
              </div>
            )}
          </div>

          {trendData.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={trendData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="sriGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor="#3b82f6" stopOpacity={0.25} />
                    <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#1a2744" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fill: '#475569', fontSize: 10 }}
                  tickLine={false}
                  axisLine={false}
                  interval="preserveStartEnd"
                />
                <YAxis
                  domain={[0, 100]}
                  tick={{ fill: '#475569', fontSize: 10 }}
                  tickLine={false}
                  axisLine={false}
                  width={28}
                />
                <Tooltip content={<SRITooltip />} />
                <Area
                  type="monotone"
                  dataKey="sri"
                  stroke="#3b82f6"
                  strokeWidth={2}
                  fill="url(#sriGradient)"
                  dot={false}
                  activeDot={{ r: 4, fill: '#60a5fa', strokeWidth: 0 }}
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[180px] flex flex-col items-center justify-center gap-2">
              <div className="w-10 h-10 rounded-full bg-slate-800 flex items-center justify-center">
                <TrendingUp className="w-5 h-5 text-slate-600" />
              </div>
              <p className="text-slate-500 text-sm">No data yet</p>
              <p className="text-slate-600 text-xs">Run a scan to generate evaluations</p>
            </div>
          )}
        </GlowCard>

        {/* Pending reviews panel */}
        <GlowCard
          color={pendingReviews.length > 0 ? 'amber' : 'slate'}
          intensity={pendingReviews.length > 0 ? 'medium' : 'low'}
          className="p-5"
        >
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold text-slate-300">Pending reviews</h2>
            {pendingReviews.length > 0 && (
              <button
                onClick={() => navigate('/decisions')}
                className="text-xs text-blue-400 hover:text-blue-300 transition-colors"
              >
                View all
              </button>
            )}
          </div>

          {pendingReviews.length === 0 ? (
            <div className="text-center py-8">
              <div className="w-12 h-12 rounded-full bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center mx-auto mb-3">
                <CheckCircle className="w-6 h-6 text-emerald-400" />
              </div>
              <p className="text-sm text-slate-300 font-medium">All clear</p>
              <p className="text-xs text-slate-600 mt-1">No actions require review</p>
            </div>
          ) : (
            <div className="space-y-2 max-h-52 overflow-y-auto pr-0.5">
              {pendingReviews.slice(0, 5).map(r => (
                <PendingCard key={r.execution_id} review={r} onNavigate={navigate} />
              ))}
              {pendingReviews.length > 5 && (
                <button
                  onClick={() => navigate('/decisions')}
                  className="w-full py-2 text-xs text-slate-500 hover:text-slate-300 transition-colors text-center"
                >
                  +{pendingReviews.length - 5} more
                </button>
              )}
            </div>
          )}
        </GlowCard>
      </div>

      {/* ── Triage Intelligence (Phase 26/27A) ── */}
      {/* Hidden for demo — enable by removing false below */}
      {false && metrics && (
        <GlowCard color="teal" intensity="low" className="p-5">
          <div className="flex items-center gap-2 mb-4">
            <Cpu className="w-4 h-4 text-teal-400" />
            <h2 className="text-sm font-semibold text-slate-300">Risk Triage Intelligence</h2>
            <span className="ml-auto text-[10px] text-slate-600 font-mono uppercase tracking-wider">Phase 27A active</span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            {/* LLM calls saved */}
            <div className="flex flex-col gap-1">
              <span className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold">LLM calls saved</span>
              <span className="text-2xl font-bold text-teal-400 tabular-nums font-data">
                <NumberTicker value={metrics.triage?.llm_calls_saved ?? 0} />
              </span>
              <span className="text-[11px] text-slate-600">by Tier 1 routing</span>
            </div>

            {/* Tier 1 — deterministic */}
            <div className="flex flex-col gap-1">
              <span className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold">Tier 1 — Skip LLM</span>
              <div className="flex items-baseline gap-1.5">
                <span className="text-2xl font-bold text-emerald-400 tabular-nums font-data">
                  <NumberTicker value={metrics.triage?.tier_counts?.tier_1 ?? 0} />
                </span>
                <span className="text-xs text-slate-500">
                  {metrics.triage?.tier_percentages?.tier_1 != null
                    ? `${metrics.triage.tier_percentages.tier_1}%`
                    : ''}
                </span>
              </div>
              <span className="text-[11px] text-slate-600">non-prod · isolated</span>
            </div>

            {/* Tier 2 — single LLM */}
            <div className="flex flex-col gap-1">
              <span className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold">Tier 2 — 1 LLM</span>
              <div className="flex items-baseline gap-1.5">
                <span className="text-2xl font-bold text-amber-400 tabular-nums font-data">
                  <NumberTicker value={metrics.triage?.tier_counts?.tier_2 ?? 0} />
                </span>
                <span className="text-xs text-slate-500">
                  {metrics.triage?.tier_percentages?.tier_2 != null
                    ? `${metrics.triage.tier_percentages.tier_2}%`
                    : ''}
                </span>
              </div>
              <span className="text-[11px] text-slate-600">prod · service scope</span>
            </div>

            {/* Tier 3 — full pipeline */}
            <div className="flex flex-col gap-1">
              <span className="text-[10px] text-slate-500 uppercase tracking-wide font-semibold">Tier 3 — Full</span>
              <div className="flex items-baseline gap-1.5">
                <span className="text-2xl font-bold text-rose-400 tabular-nums font-data">
                  <NumberTicker value={metrics.triage?.tier_counts?.tier_3 ?? 0} />
                </span>
                <span className="text-xs text-slate-500">
                  {metrics.triage?.tier_percentages?.tier_3 != null
                    ? `${metrics.triage.tier_percentages.tier_3}%`
                    : ''}
                </span>
              </div>
              <span className="text-[11px] text-slate-600">compliance · network</span>
            </div>
          </div>

          {/* Progress bar */}
          {metrics.total_evaluations > 0 && (
            <div className="mt-4 pt-3 border-t border-slate-800/60">
              <div className="flex gap-0.5 h-1.5 rounded-full overflow-hidden bg-slate-800">
                {(metrics.triage?.tier_percentages?.tier_1 ?? 0) > 0 && (
                  <div
                    className="bg-emerald-500/70 transition-all duration-700"
                    style={{ width: `${metrics.triage.tier_percentages.tier_1}%` }}
                  />
                )}
                {(metrics.triage?.tier_percentages?.tier_2 ?? 0) > 0 && (
                  <div
                    className="bg-amber-500/70 transition-all duration-700"
                    style={{ width: `${metrics.triage.tier_percentages.tier_2}%` }}
                  />
                )}
                {(metrics.triage?.tier_percentages?.tier_3 ?? 0) > 0 && (
                  <div
                    className="bg-rose-500/70 transition-all duration-700"
                    style={{ width: `${metrics.triage.tier_percentages.tier_3}%` }}
                  />
                )}
              </div>
              <div className="flex gap-4 mt-1.5">
                <span className="text-[10px] text-emerald-600 flex items-center gap-1">
                  <span className="inline-block w-2 h-1 rounded-sm bg-emerald-500/70" /> Tier 1
                </span>
                <span className="text-[10px] text-amber-600 flex items-center gap-1">
                  <span className="inline-block w-2 h-1 rounded-sm bg-amber-500/70" /> Tier 2
                </span>
                <span className="text-[10px] text-rose-600 flex items-center gap-1">
                  <span className="inline-block w-2 h-1 rounded-sm bg-rose-500/70" /> Tier 3
                </span>
              </div>
            </div>
          )}
        </GlowCard>
      )}

      {/* ── Recent scan runs ── */}
      <GlowCard color="slate" intensity="low" className="p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold text-slate-300">Recent scan runs</h2>
          <div className="flex items-center gap-3">
            <button
              onClick={loadScans}
              className="text-slate-600 hover:text-slate-300 transition-colors"
              title="Refresh scan history"
            >
              <RefreshCw className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={() => navigate('/scans')}
              className="text-xs text-blue-400 hover:text-blue-300 transition-colors"
            >
              Run scans →
            </button>
          </div>
        </div>

        {scansLoading ? (
          <table className="w-full">
            <TableSkeleton rows={3} cols={7} />
          </table>
        ) : recentScans.length === 0 ? (
          <div className="text-center py-6">
            <p className="text-slate-500 text-sm">No scan history yet.</p>
            <button
              onClick={() => navigate('/scans')}
              className="text-blue-400 hover:text-blue-300 text-sm underline underline-offset-2 mt-1"
            >
              Run your first scan
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-800/80">
                  {['Scan ID', 'Agent', 'Started', 'Duration', 'Proposals', 'Verdicts', 'Result'].map((h, i) => (
                    <th
                      key={h}
                      className={`pb-2.5 ${i >= 4 ? 'text-right' : 'text-left'} pr-4 last:pr-0 text-[11px] font-semibold text-slate-500 uppercase tracking-wide`}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60">
                {recentScans.map((scan, i) => (
                  <tr key={i} className="hover:bg-slate-800/30 transition-colors group">
                    <td className="py-3 pr-4 font-mono text-xs text-slate-600 group-hover:text-slate-400 transition-colors">
                      {scan.scan_id ? `${scan.scan_id.slice(0, 8)}…` : '—'}
                    </td>
                    <td className="py-3 pr-4">
                      <span className="text-blue-400 font-mono text-xs font-medium">
                        {AGENT_TYPE_LABELS[scan.agent_type] ?? AGENT_LABELS[scan.source] ?? scan.agent_type ?? scan.source}
                      </span>
                    </td>
                    <td className="py-3 pr-4 text-xs text-slate-400">{formatTime(scan.started_at)}</td>
                    <td className="py-3 pr-4 text-xs text-slate-400 tabular-nums">
                      {scanDuration(scan.started_at, scan.completed_at) ?? '—'}
                    </td>
                    <td className="py-3 pr-4 text-right text-xs text-slate-400 tabular-nums">
                      {scan.proposals_count ?? 0}
                    </td>
                    <td className="py-3 pr-4 text-right">
                      {(scan.evaluations_count ?? 0) > 0 ? (
                        <span className="text-xs font-semibold text-amber-400 tabular-nums">
                          {scan.evaluations_count}
                        </span>
                      ) : (
                        <span className="text-xs text-slate-700">0</span>
                      )}
                    </td>
                    <td className="py-3">
                      {scan.status === 'complete' && (scan.evaluations_count ?? 0) === 0 ? (
                        <span className="text-xs text-emerald-400 flex items-center gap-1">
                          <CheckCircle className="w-3 h-3" /> Clean
                        </span>
                      ) : scan.status === 'complete' ? (
                        <span className="text-xs text-blue-400 flex items-center gap-1">
                          <CheckCircle className="w-3 h-3" /> Complete
                        </span>
                      ) : (
                        <span className="text-xs text-amber-400">{scan.status ?? '—'}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </GlowCard>

    </div>
  )
}
