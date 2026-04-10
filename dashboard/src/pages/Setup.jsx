/**
 * Setup.jsx — first-time admin account creation screen.
 *
 * Shown when GET /api/auth/status returns { setup_required: true }.
 * After successful creation, calls onLogin(token, username) so the
 * parent AuthGate transitions directly to the main dashboard — no
 * second login step needed.
 */

import React, { useState } from 'react'
import { Shield, Eye, EyeOff, UserPlus, CheckCircle } from 'lucide-react'
import { authSetup, setToken } from '../api'

function PasswordStrength({ password }) {
  if (!password) return null
  const checks = [
    { label: '8+ characters', ok: password.length >= 8 },
    { label: 'Uppercase letter', ok: /[A-Z]/.test(password) },
    { label: 'Number or symbol', ok: /[0-9!@#$%^&*]/.test(password) },
  ]
  return (
    <div className="mt-2 space-y-1">
      {checks.map(c => (
        <div key={c.label} className="flex items-center gap-1.5 text-xs">
          <CheckCircle
            className="w-3 h-3 shrink-0"
            style={{ color: c.ok ? '#34d399' : 'rgba(100,116,139,0.5)' }}
          />
          <span style={{ color: c.ok ? '#94a3b8' : 'rgba(100,116,139,0.5)' }}>{c.label}</span>
        </div>
      ))}
    </div>
  )
}

export default function Setup({ onLogin }) {
  const [username,  setUsername]  = useState('')
  const [password,  setPassword]  = useState('')
  const [confirm,   setConfirm]   = useState('')
  const [showPw,    setShowPw]    = useState(false)
  const [showCon,   setShowCon]   = useState(false)
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState(null)

  const passwordsMatch = password === confirm
  const canSubmit = username.trim().length > 0 && password.length >= 8 && passwordsMatch

  async function handleSubmit(e) {
    e.preventDefault()
    if (!canSubmit) return
    if (!passwordsMatch) { setError('Passwords do not match.'); return }
    setLoading(true)
    setError(null)
    try {
      const { token, username: user } = await authSetup(username.trim(), password)
      setToken(token)
      onLogin(token, user)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center p-4"
      style={{ background: 'var(--bg-base)' }}
    >
      {/* Subtle background grid */}
      <div
        className="fixed inset-0 pointer-events-none"
        style={{
          backgroundImage:
            'linear-gradient(rgba(30,45,74,0.25) 1px, transparent 1px),' +
            'linear-gradient(90deg, rgba(30,45,74,0.25) 1px, transparent 1px)',
          backgroundSize: '48px 48px',
        }}
      />

      <div className="relative w-full max-w-sm">
        {/* Logo */}
        <div className="text-center mb-8">
          <div
            className="inline-flex items-center justify-center w-16 h-16 rounded-2xl mb-4"
            style={{
              background: 'linear-gradient(135deg, rgba(59,130,246,0.2), rgba(139,92,246,0.2))',
              border: '1px solid rgba(59,130,246,0.3)',
              boxShadow: '0 0 32px rgba(59,130,246,0.15)',
            }}
          >
            <Shield className="w-8 h-8 text-blue-400" />
          </div>
          <h1
            className="text-2xl font-bold tracking-tight"
            style={{ color: 'var(--text-primary)' }}
          >
            RuriSkry
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            AI Governance Dashboard
          </p>
        </div>

        {/* Card */}
        <div
          className="rounded-2xl p-8"
          style={{
            background: 'var(--bg-card)',
            border: '1px solid rgba(30,45,74,0.8)',
            boxShadow: '0 0 0 1px rgba(59,130,246,0.06), 0 24px 48px rgba(0,0,0,0.4)',
          }}
        >
          {/* Welcome banner */}
          <div
            className="rounded-xl px-4 py-3 mb-6"
            style={{
              background: 'linear-gradient(135deg, rgba(59,130,246,0.1), rgba(139,92,246,0.08))',
              border: '1px solid rgba(59,130,246,0.2)',
            }}
          >
            <p className="text-sm font-medium text-blue-300">Welcome — first-time setup</p>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              Create the admin account to secure this dashboard. This screen
              will not appear again after setup is complete.
            </p>
          </div>

          <h2
            className="text-lg font-semibold mb-6"
            style={{ color: 'var(--text-primary)' }}
          >
            Create admin account
          </h2>

          <form onSubmit={handleSubmit} className="space-y-4">
            {/* Username */}
            <div>
              <label
                className="block text-xs font-medium mb-1.5"
                style={{ color: 'var(--text-secondary)' }}
              >
                Username
              </label>
              <input
                type="text"
                autoComplete="username"
                value={username}
                onChange={e => setUsername(e.target.value)}
                className="w-full px-3 py-2 rounded-lg text-sm outline-none transition-colors"
                style={{
                  background: 'rgba(15,23,42,0.8)',
                  border: '1px solid rgba(30,45,74,0.9)',
                  color: 'var(--text-primary)',
                }}
                onFocus={e => (e.target.style.borderColor = 'rgba(59,130,246,0.6)')}
                onBlur={e => (e.target.style.borderColor = 'rgba(30,45,74,0.9)')}
                placeholder="Choose a username"
                required
              />
            </div>

            {/* Password */}
            <div>
              <label
                className="block text-xs font-medium mb-1.5"
                style={{ color: 'var(--text-secondary)' }}
              >
                Password
              </label>
              <div className="relative">
                <input
                  type={showPw ? 'text' : 'password'}
                  autoComplete="new-password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  className="w-full px-3 py-2 pr-10 rounded-lg text-sm outline-none transition-colors"
                  style={{
                    background: 'rgba(15,23,42,0.8)',
                    border: '1px solid rgba(30,45,74,0.9)',
                    color: 'var(--text-primary)',
                  }}
                  onFocus={e => (e.target.style.borderColor = 'rgba(59,130,246,0.6)')}
                  onBlur={e => (e.target.style.borderColor = 'rgba(30,45,74,0.9)')}
                  placeholder="Minimum 8 characters"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPw(p => !p)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 p-0.5 rounded"
                  style={{ color: 'var(--text-muted)' }}
                  tabIndex={-1}
                >
                  {showPw ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
              <PasswordStrength password={password} />
            </div>

            {/* Confirm password */}
            <div>
              <label
                className="block text-xs font-medium mb-1.5"
                style={{ color: 'var(--text-secondary)' }}
              >
                Confirm password
              </label>
              <div className="relative">
                <input
                  type={showCon ? 'text' : 'password'}
                  autoComplete="new-password"
                  value={confirm}
                  onChange={e => setConfirm(e.target.value)}
                  className="w-full px-3 py-2 pr-10 rounded-lg text-sm outline-none transition-colors"
                  style={{
                    background: 'rgba(15,23,42,0.8)',
                    border: confirm
                      ? passwordsMatch
                        ? '1px solid rgba(52,211,153,0.5)'
                        : '1px solid rgba(239,68,68,0.5)'
                      : '1px solid rgba(30,45,74,0.9)',
                    color: 'var(--text-primary)',
                  }}
                  onFocus={e => (e.target.style.borderColor = 'rgba(59,130,246,0.6)')}
                  onBlur={e => {
                    if (confirm) {
                      e.target.style.borderColor = passwordsMatch
                        ? 'rgba(52,211,153,0.5)'
                        : 'rgba(239,68,68,0.5)'
                    } else {
                      e.target.style.borderColor = 'rgba(30,45,74,0.9)'
                    }
                  }}
                  placeholder="Re-enter password"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowCon(p => !p)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 p-0.5 rounded"
                  style={{ color: 'var(--text-muted)' }}
                  tabIndex={-1}
                >
                  {showCon ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
              {confirm && !passwordsMatch && (
                <p className="text-xs mt-1.5" style={{ color: '#f87171' }}>
                  Passwords do not match
                </p>
              )}
            </div>

            {/* Error */}
            {error && (
              <div
                className="rounded-lg px-3 py-2 text-sm"
                style={{
                  background: 'rgba(239,68,68,0.1)',
                  border: '1px solid rgba(239,68,68,0.25)',
                  color: '#f87171',
                }}
              >
                {error}
              </div>
            )}

            {/* Submit */}
            <button
              type="submit"
              disabled={loading || !canSubmit}
              className="w-full flex items-center justify-center gap-2 py-2.5 rounded-lg text-sm font-medium transition-all mt-2"
              style={{
                background: loading
                  ? 'rgba(59,130,246,0.4)'
                  : 'linear-gradient(135deg, #3b82f6, #6366f1)',
                color: '#fff',
                opacity: !canSubmit ? 0.5 : 1,
                cursor: loading || !canSubmit ? 'not-allowed' : 'pointer',
              }}
            >
              {loading ? (
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : (
                <UserPlus className="w-4 h-4" />
              )}
              {loading ? 'Creating account…' : 'Create account'}
            </button>
          </form>
        </div>

        <p className="text-center text-xs mt-6" style={{ color: 'var(--text-muted)' }}>
          RuriSkry AI Governance Engine
        </p>
      </div>
    </div>
  )
}
