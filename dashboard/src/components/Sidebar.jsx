/**
 * Sidebar.jsx — left navigation panel.
 *
 * Phase 24 / Ops Nerve Center redesign:
 *   - DM Sans UI text, JetBrains Mono for version string
 *   - Teal breathing glow on the SL logo (animate-breathe)
 *   - Animated left-bar active indicator
 *   - Amber icon urgency pulse on Decisions when reviews are pending
 *   - "System online" live indicator at bottom
 */

import React from 'react'
import { NavLink } from 'react-router-dom'
import { LayoutDashboard, ScanLine, Bot, ShieldCheck, FileText, Activity, Zap, Settings } from 'lucide-react'

const NAV = [
  { to: '/overview',  icon: LayoutDashboard, label: 'Overview' },
  { to: '/scans',     icon: ScanLine,        label: 'Scans' },
  { to: '/alerts',    icon: Zap,             label: 'Alerts' },
  { to: '/agents',    icon: Bot,             label: 'Agents' },
  { to: '/decisions', icon: ShieldCheck,     label: 'Decisions' },
  { to: '/audit',     icon: FileText,        label: 'Audit Log' },
]

export default function Sidebar({ pendingCount = 0, alertCount = 0 }) {
  return (
    <aside
      className="w-56 shrink-0 flex flex-col"
      style={{
        background: 'linear-gradient(180deg, #040e22 0%, var(--bg-sidebar) 100%)',
        borderRight: '1px solid var(--border-subtle)',
      }}
    >
      {/* ── Logo ── */}
      <div className="p-5" style={{ borderBottom: '1px solid var(--border-subtle)' }}>
        <div className="flex items-center gap-3">
          {/* Teal breathing glow logo */}
          <div
            className="w-8 h-8 rounded-lg flex items-center justify-center font-black text-white text-xs shrink-0 animate-breathe"
            style={{
              background: 'linear-gradient(135deg, #14b8a6 0%, #0d9488 100%)',
              fontFamily: 'var(--font-data)',
              letterSpacing: '0.05em',
            }}
          >
            SL
          </div>
          <div>
            <div
              className="text-sm font-semibold leading-none tracking-tight text-white"
              style={{ letterSpacing: '-0.025em' }}
            >
              RuriSkry
            </div>
            <div className="text-[11px] mt-0.5" style={{ color: 'rgba(100,116,139,0.7)', fontFamily: 'var(--font-data)' }}>
              Governance Engine
            </div>
          </div>
        </div>
      </div>

      {/* ── Nav section label ── */}
      <div className="px-4 pt-5 pb-2">
        <span
          className="text-[10px] font-semibold uppercase tracking-[0.14em]"
          style={{ color: 'rgba(71,85,105,0.6)' }}
        >
          Navigation
        </span>
      </div>

      {/* ── Nav links ── */}
      <nav className="flex-1 px-2 space-y-0.5">
        {NAV.map(({ to, icon: Icon, label }) => {
          const isDecisions = to === '/decisions'
          const showUrgent  = isDecisions && pendingCount > 0

          return (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `relative group flex items-center justify-between px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-200 ${
                  isActive ? 'text-blue-300' : 'text-slate-500 hover:text-slate-200'
                }`
              }
              style={({ isActive }) => isActive ? {
                background: 'rgba(59,130,246,0.08)',
                boxShadow: 'inset 0 0 0 1px rgba(59,130,246,0.15)',
              } : {}}
            >
              {({ isActive }) => (
                <>
                  {/* Active left indicator bar */}
                  {isActive && (
                    <span
                      className="nav-active-indicator absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 rounded-r-full"
                      style={{
                        background: 'linear-gradient(180deg, #93c5fd, #3b82f6)',
                        boxShadow: '0 0 8px rgba(59,130,246,0.7)',
                      }}
                    />
                  )}

                  <div className="flex items-center gap-3 pl-0.5">
                    <Icon
                      className={`w-4 h-4 shrink-0 transition-colors ${
                        showUrgent
                          ? 'animate-icon-urgent'
                          : isActive
                          ? 'text-blue-400'
                          : 'text-slate-600 group-hover:text-slate-400'
                      }`}
                    />
                    <span>{label}</span>
                  </div>

                  {/* Active alerts badge */}
                  {to === '/alerts' && alertCount > 0 && (
                    <span
                      className="text-[10px] font-bold text-white rounded-full px-1.5 min-w-[18px] text-center leading-[18px] font-mono"
                      style={{
                        background: 'linear-gradient(135deg, #ef4444, #dc2626)',
                        boxShadow: '0 0 10px rgba(239,68,68,0.6)',
                        fontFamily: 'var(--font-data)',
                      }}
                    >
                      {alertCount}
                    </span>
                  )}

                  {/* Pending badge on Decisions */}
                  {to === '/decisions' && pendingCount > 0 && (
                    <span
                      className="text-[10px] font-bold text-white rounded-full px-1.5 min-w-[18px] text-center leading-[18px] font-mono"
                      style={{
                        background: 'linear-gradient(135deg, #f59e0b, #d97706)',
                        boxShadow: '0 0 10px rgba(245,158,11,0.6)',
                        fontFamily: 'var(--font-data)',
                      }}
                    >
                      {pendingCount}
                    </span>
                  )}

                  {/* Overview pending badge */}
                  {to === '/overview' && pendingCount > 0 && (
                    <span
                      className="text-[10px] font-bold text-white rounded-full px-1.5 min-w-[18px] text-center leading-[18px]"
                      style={{
                        background: 'rgba(245,158,11,0.2)',
                        border: '1px solid rgba(245,158,11,0.4)',
                        color: '#fcd34d',
                        fontFamily: 'var(--font-data)',
                      }}
                    >
                      {pendingCount}
                    </span>
                  )}
                </>
              )}
            </NavLink>
          )
        })}
      </nav>

      {/* ── Admin link ── */}
      <div className="px-2 pb-2">
        <NavLink
          to="/admin"
          className={({ isActive }) =>
            `relative group flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-all duration-200 ${
              isActive ? 'text-slate-300 bg-slate-800/50' : 'text-slate-600 hover:text-slate-400'
            }`
          }
        >
          {({ isActive }) => (
            <>
              {isActive && (
                <span
                  className="nav-active-indicator absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 rounded-r-full"
                  style={{
                    background: 'linear-gradient(180deg, #94a3b8, #64748b)',
                    boxShadow: '0 0 6px rgba(100,116,139,0.5)',
                  }}
                />
              )}
              <Settings className={`w-4 h-4 shrink-0 transition-colors ${isActive ? 'text-slate-400' : 'text-slate-700 group-hover:text-slate-500'}`} />
              <span>Admin</span>
            </>
          )}
        </NavLink>
      </div>

      {/* ── System status indicator ── */}
      <div
        className="mx-3 mb-3 rounded-lg p-3"
        style={{ background: 'rgba(16,185,129,0.04)', border: '1px solid rgba(16,185,129,0.1)' }}
      >
        <div className="flex items-center gap-2">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-40" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
          </span>
          <span className="text-[11px] text-emerald-400/70 font-medium">System online</span>
        </div>
      </div>

      {/* ── Footer ── */}
      <div className="px-4 pb-4" style={{ borderTop: '1px solid var(--border-subtle)' }}>
        <div className="pt-3">
          <div
            className="text-[11px]"
            style={{ color: 'rgba(71,85,105,0.55)', fontFamily: 'var(--font-data)' }}
          >
            SRI™ Engine v1.0
          </div>
          <div className="text-[10px] mt-0.5 flex items-center gap-1" style={{ color: 'rgba(51,65,85,0.5)' }}>
            <Activity className="w-2.5 h-2.5" />
            {new Date().getFullYear()}
          </div>
        </div>
      </div>
    </aside>
  )
}
