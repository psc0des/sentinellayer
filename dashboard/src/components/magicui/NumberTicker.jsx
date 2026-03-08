/**
 * NumberTicker — animates a number counting up when value changes.
 * Inspired by Magic UI's NumberTicker component.
 *
 * Props:
 *   value     — numeric value (or string like "—" for empty state)
 *   decimals  — decimal places to display (default 0)
 *   suffix    — string appended after number (e.g. "%")
 *   prefix    — string prepended before number
 *   duration  — animation duration in ms (default 1200)
 *   className — extra Tailwind classes
 */

import { useEffect, useRef, useState } from 'react'

export default function NumberTicker({
  value,
  decimals = 0,
  suffix = '',
  prefix = '',
  duration = 1200,
  className = '',
}) {
  const numVal = typeof value === 'number' ? value : parseFloat(String(value ?? ''))
  const isValid = !isNaN(numVal)

  const [display, setDisplay] = useState(isValid ? numVal : 0)
  const rafRef = useRef(null)
  const prevRef = useRef(isValid ? numVal : 0)
  const mountedRef = useRef(false)

  useEffect(() => {
    if (!isValid) return

    // On first mount — show value immediately without animation
    if (!mountedRef.current) {
      mountedRef.current = true
      prevRef.current = numVal
      setDisplay(numVal)
      return
    }

    const from = prevRef.current
    const to = numVal
    const startTime = { t: null }

    if (rafRef.current) cancelAnimationFrame(rafRef.current)

    const tick = (ts) => {
      if (!startTime.t) startTime.t = ts
      const elapsed = ts - startTime.t
      const t = Math.min(elapsed / duration, 1)
      // easeOutQuart: rapid start, smooth deceleration
      const eased = 1 - Math.pow(1 - t, 4)
      setDisplay(from + (to - from) * eased)
      if (t < 1) {
        rafRef.current = requestAnimationFrame(tick)
      } else {
        prevRef.current = to
        rafRef.current = null
      }
    }

    rafRef.current = requestAnimationFrame(tick)
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
    }
  }, [numVal, duration, isValid])

  if (!isValid) return <span className={className}>—</span>

  return (
    <span className={`tabular-nums ${className}`}>
      {prefix}{display.toFixed(decimals)}{suffix}
    </span>
  )
}
