/**
 * Login.jsx — credential form shown when a session token is not present
 * or has expired. On success it calls onLogin(token, username) so the
 * parent AuthGate can update state and render the main AppShell.
 */

import React, { useState } from 'react'
import { Shield, Eye, EyeOff, LogIn } from 'lucide-react'
import { authLogin, setToken } from '../api'

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPw,   setShowPw]   = useState(false)
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    if (!username.trim() || !password) return
    setLoading(true)
    setError(null)
    try {
      const { token, username: user } = await authLogin(username.trim(), password)
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
          <h2
            className="text-lg font-semibold mb-6"
            style={{ color: 'var(--text-primary)' }}
          >
            Sign in
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
                placeholder="admin"
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
                  autoComplete="current-password"
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
                  placeholder="••••••••"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPw(p => !p)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 p-0.5 rounded transition-colors"
                  style={{ color: 'var(--text-muted)' }}
                  tabIndex={-1}
                >
                  {showPw ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
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
              disabled={loading || !username.trim() || !password}
              className="w-full flex items-center justify-center gap-2 py-2.5 rounded-lg text-sm font-medium transition-all"
              style={{
                background: loading
                  ? 'rgba(59,130,246,0.4)'
                  : 'linear-gradient(135deg, #3b82f6, #6366f1)',
                color: '#fff',
                opacity: !username.trim() || !password ? 0.5 : 1,
                cursor: loading || !username.trim() || !password ? 'not-allowed' : 'pointer',
              }}
            >
              {loading ? (
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : (
                <LogIn className="w-4 h-4" />
              )}
              {loading ? 'Signing in…' : 'Sign in'}
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
