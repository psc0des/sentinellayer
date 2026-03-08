/**
 * GlowCard — card with glass depth, color-coded glow, optional border beam.
 *
 * Phase 24 / Ops Nerve Center:
 *   - backdrop-filter blur for genuine glass depth
 *   - Inner border line for layered surface feel
 *   - `urgent` prop → slow amber urgency pulse animation
 *   - Border beam: gradient light scanning the top edge
 */

import React from 'react'

const COLOR_CONFIG = {
  blue: {
    border:  '1px solid rgba(59,130,246,0.22)',
    inner:   'inset 0 1px 0 rgba(255,255,255,0.04)',
    shadow:  { low: '0 0 0 0 transparent', medium: '0 0 20px -4px rgba(59,130,246,0.25)', high: '0 0 35px -4px rgba(59,130,246,0.50)' },
    beam:    'rgba(59,130,246,0.9)',
  },
  green: {
    border:  '1px solid rgba(16,185,129,0.22)',
    inner:   'inset 0 1px 0 rgba(255,255,255,0.04)',
    shadow:  { low: '0 0 0 0 transparent', medium: '0 0 20px -4px rgba(16,185,129,0.25)', high: '0 0 35px -4px rgba(16,185,129,0.50)' },
    beam:    'rgba(16,185,129,0.9)',
  },
  amber: {
    border:  '1px solid rgba(245,158,11,0.22)',
    inner:   'inset 0 1px 0 rgba(255,255,255,0.04)',
    shadow:  { low: '0 0 0 0 transparent', medium: '0 0 20px -4px rgba(245,158,11,0.25)', high: '0 0 35px -4px rgba(245,158,11,0.50)' },
    beam:    'rgba(245,158,11,0.9)',
  },
  red: {
    border:  '1px solid rgba(239,68,68,0.22)',
    inner:   'inset 0 1px 0 rgba(255,255,255,0.04)',
    shadow:  { low: '0 0 0 0 transparent', medium: '0 0 20px -4px rgba(239,68,68,0.25)', high: '0 0 35px -4px rgba(239,68,68,0.50)' },
    beam:    'rgba(239,68,68,0.9)',
  },
  purple: {
    border:  '1px solid rgba(139,92,246,0.22)',
    inner:   'inset 0 1px 0 rgba(255,255,255,0.04)',
    shadow:  { low: '0 0 0 0 transparent', medium: '0 0 20px -4px rgba(139,92,246,0.25)', high: '0 0 35px -4px rgba(139,92,246,0.50)' },
    beam:    'rgba(139,92,246,0.9)',
  },
  slate: {
    border:  '1px solid rgba(30,45,74,0.7)',
    inner:   'inset 0 1px 0 rgba(255,255,255,0.03)',
    shadow:  { low: '0 0 0 0 transparent', medium: '0 0 15px -4px rgba(15,23,42,0.5)', high: '0 0 25px -4px rgba(15,23,42,0.7)' },
    beam:    'rgba(100,116,139,0.6)',
  },
}

export default function GlowCard({
  children,
  className = '',
  color = 'slate',
  intensity = 'low',
  beam = false,
  beamDuration = 3,
  urgent = false,
  as: Tag = 'div',
  style = {},
  ...props
}) {
  const cfg = COLOR_CONFIG[color] ?? COLOR_CONFIG.slate

  return (
    <Tag
      className={`relative rounded-xl transition-all duration-300 ${
        urgent ? 'animate-urgent-pulse' : ''
      } ${className}`}
      style={{
        background: 'var(--bg-surface)',
        backdropFilter: 'blur(12px)',
        WebkitBackdropFilter: 'blur(12px)',
        border: cfg.border,
        boxShadow: `${cfg.shadow[intensity] ?? cfg.shadow.low}, ${cfg.inner}`,
        ...style,
      }}
      {...props}
    >
      {/* Border beam: gradient light scanning top edge */}
      {beam && (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 rounded-xl overflow-hidden"
        >
          <span
            className="absolute top-0 left-0 h-[1px] w-1/3"
            style={{
              background: `linear-gradient(90deg, transparent 0%, ${cfg.beam} 50%, transparent 100%)`,
              animation: `scanBeam ${beamDuration}s linear infinite`,
            }}
          />
        </span>
      )}

      {children}
    </Tag>
  )
}
