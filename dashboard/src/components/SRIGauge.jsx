/**
 * SRIGauge — semicircular SVG gauge for the SRI™ Composite score.
 *
 * Color zones:
 *   0–25   → green  (APPROVED)
 *   26–60  → yellow (ESCALATED)
 *   61–100 → red    (DENIED)
 *
 * The gauge arc goes from the left (value=0) clockwise through the top
 * to the right (value=100).  A needle and filled arc show the current score.
 */

import React from 'react'

// ── SVG coordinate constants ──────────────────────────────────────────────
const CX = 100   // centre x
const CY = 95    // centre y (shifted up slightly so score text fits below)
const R  = 70    // arc radius
const SW = 12    // stroke width of the arc track

// ── Helpers ───────────────────────────────────────────────────────────────

/** Map a score (0–100) to an angle in radians.
 *  score=0  → π   (leftmost point)
 *  score=100 → 0  (rightmost point) */
function valueToAngle(v) {
  return Math.PI * (1 - v / 100)
}

/** Convert polar angle + radius to an SVG {x, y} point.
 *  Note: SVG y-axis increases downward, so we subtract the y component. */
function pt(angle, r = R) {
  return {
    x: CX + r * Math.cos(angle),
    y: CY - r * Math.sin(angle),
  }
}

/**
 * Build an SVG arc path from value `from` to value `to`.
 * sweep=1 = clockwise in screen coords = arc goes UP through the top.
 * large-arc is always 0 because our arcs never exceed 180°.
 */
function arcPath(from, to) {
  if (from >= to) return ''
  const s = pt(valueToAngle(from))
  const e = pt(valueToAngle(to))
  return `M ${s.x.toFixed(2)} ${s.y.toFixed(2)} A ${R} ${R} 0 0 1 ${e.x.toFixed(2)} ${e.y.toFixed(2)}`
}

/** Pick the right colour for a score. */
function sriColor(score) {
  if (score <= 25) return '#22c55e'   // green-500
  if (score <= 60) return '#eab308'   // yellow-500
  return '#ef4444'                    // red-500
}

// ── Component ─────────────────────────────────────────────────────────────

export default function SRIGauge({ score }) {
  const v = Math.max(0, Math.min(100, score ?? 0))
  const color = sriColor(v)

  // Needle tip sits just inside the arc track
  const needleTip = pt(valueToAngle(v), R - 8)

  // Coloured zone definitions
  const zones = [
    { from: 0,  to: 25,  color: '#22c55e' },
    { from: 25, to: 60,  color: '#eab308' },
    { from: 60, to: 100, color: '#ef4444' },
  ]

  // Tick marks at every zone boundary
  const ticks = [0, 25, 60, 100]

  // Zone centre-point labels
  const zoneLabels = [
    { midValue: 12,  text: 'SAFE',   color: '#22c55e' },
    { midValue: 42,  text: 'REVIEW', color: '#eab308' },
    { midValue: 80,  text: 'HIGH',   color: '#ef4444' },
  ]

  return (
    <div className="flex flex-col items-center">
      {/* viewBox gives just enough room: x 0–200, y 15–130 */}
      <svg viewBox="0 15 200 120" className="w-60 drop-shadow-lg" aria-label={`SRI score ${v.toFixed(1)}`}>

        {/* ── Background zone tracks (dim) ── */}
        {zones.map(z => (
          <path
            key={z.from}
            d={arcPath(z.from, z.to)}
            fill="none"
            stroke={z.color}
            strokeWidth={SW}
            strokeLinecap="round"
            opacity={0.18}
          />
        ))}

        {/* ── Filled arc up to current score ── */}
        {v > 0.5 && (
          <path
            d={arcPath(0, v)}
            fill="none"
            stroke={color}
            strokeWidth={SW}
            strokeLinecap="round"
          />
        )}

        {/* ── Tick marks at zone boundaries ── */}
        {ticks.map(tick => {
          const a = valueToAngle(tick)
          const outer = pt(a, R + SW / 2 + 3)
          const inner = pt(a, R - SW / 2 - 3)
          return (
            <line
              key={tick}
              x1={outer.x.toFixed(2)} y1={outer.y.toFixed(2)}
              x2={inner.x.toFixed(2)} y2={inner.y.toFixed(2)}
              stroke="#475569"
              strokeWidth={1.5}
            />
          )
        })}

        {/* ── Zone labels ── */}
        {zoneLabels.map(({ midValue, text, color: c }) => {
          const labelPos = pt(valueToAngle(midValue), R + SW + 11)
          return (
            <text
              key={text}
              x={labelPos.x.toFixed(2)}
              y={labelPos.y.toFixed(2)}
              textAnchor="middle"
              fontSize="6.5"
              fill={c}
              opacity={0.75}
              fontWeight="600"
            >
              {text}
            </text>
          )
        })}

        {/* ── Needle ── */}
        <line
          x1={CX} y1={CY}
          x2={needleTip.x.toFixed(2)} y2={needleTip.y.toFixed(2)}
          stroke={color}
          strokeWidth={2.5}
          strokeLinecap="round"
        />

        {/* ── Centre dot ── */}
        <circle cx={CX} cy={CY} r={5} fill={color} />
        <circle cx={CX} cy={CY} r={2.5} fill="#0f172a" />

        {/* ── Score text ── */}
        <text
          x={CX} y={CY + 20}
          textAnchor="middle"
          fontSize="22"
          fontWeight="bold"
          fill={color}
        >
          {v.toFixed(1)}
        </text>
        <text
          x={CX} y={CY + 31}
          textAnchor="middle"
          fontSize="7"
          fill="#64748b"
          letterSpacing="1"
        >
          SRI™ COMPOSITE
        </text>
      </svg>

      {/* Colour legend strip below the gauge */}
      <div className="flex gap-4 text-xs mt-1">
        <span className="text-green-400 font-semibold">≤25 Approved</span>
        <span className="text-yellow-400 font-semibold">26–60 Review</span>
        <span className="text-red-400 font-semibold">&gt;60 Denied</span>
      </div>
    </div>
  )
}
