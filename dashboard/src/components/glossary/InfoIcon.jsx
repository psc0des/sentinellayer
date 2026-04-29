/**
 * InfoIcon.jsx — inline contextual-help trigger.
 *
 * Renders a small "i" icon that opens a popover with a glossary term's
 * short definition + a "Learn more" link to the full Glossary page.
 *
 * Single source of truth: dashboard/src/data/glossary.json.
 * If a termId does not resolve, the component renders nothing and warns
 * in dev — missing terms are visible but never crash production.
 *
 * Behaviour:
 *   - Click toggles popover (no hover trigger — works on mobile, no accidents)
 *   - ESC closes; click outside closes
 *   - Learn-more deep-links to /glossary#${termId}
 *   - Keyboard accessible: button is focusable and Enter/Space activates
 *
 * Props:
 *   termId    — required string id from glossary.json
 *   size      — icon px size (default 14)
 *   className — extra classes appended to the button
 *   ariaLabelOverride — optional override for the button's aria-label
 */

import React, { useEffect, useRef, useState, useId } from 'react'
import { Info } from 'lucide-react'
import { Link } from 'react-router-dom'
import glossary from '../../data/glossary.json'

const TERM_INDEX = Object.fromEntries(glossary.map((t) => [t.id, t]))

export default function InfoIcon({ termId, size = 14, className = '', ariaLabelOverride = null }) {
  const [open, setOpen] = useState(false)
  const containerRef = useRef(null)
  const buttonRef = useRef(null)
  const popoverId = useId()

  const term = TERM_INDEX[termId]

  useEffect(() => {
    if (!term && termId) {
      console.warn(`[InfoIcon] termId "${termId}" not found in glossary.json`)
    }
  }, [term, termId])

  useEffect(() => {
    if (!open) return undefined

    function onKeyDown(e) {
      if (e.key === 'Escape') {
        setOpen(false)
        buttonRef.current?.focus()
      }
    }
    function onPointerDown(e) {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false)
      }
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('pointerdown', onPointerDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('pointerdown', onPointerDown)
    }
  }, [open])

  if (!term) return null

  const ariaLabel = ariaLabelOverride || `Definition of ${term.term}`

  return (
    <span ref={containerRef} className="relative inline-flex items-center align-middle">
      <button
        ref={buttonRef}
        type="button"
        onClick={(e) => {
          e.stopPropagation()
          e.preventDefault()
          setOpen((v) => !v)
        }}
        aria-label={ariaLabel}
        aria-expanded={open}
        aria-controls={open ? popoverId : undefined}
        className={`inline-flex items-center justify-center rounded-full text-slate-400 hover:text-teal-400 focus:text-teal-400 focus:outline-none focus:ring-1 focus:ring-teal-500/40 transition-colors ${className}`}
        style={{ width: size + 4, height: size + 4 }}
      >
        <Info style={{ width: size, height: size }} />
      </button>

      {open && (
        <div
          id={popoverId}
          role="dialog"
          aria-label={`${term.term} definition`}
          className="absolute z-50 left-1/2 -translate-x-1/2 top-full mt-2 w-72 rounded-lg border border-slate-700 bg-slate-900 shadow-xl shadow-black/40 p-3 text-left"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="text-sm font-semibold text-slate-100 mb-1">{term.term}</div>
          <div className="text-xs text-slate-300 leading-relaxed">{term.short}</div>
          <div className="mt-2 pt-2 border-t border-slate-800 flex justify-between items-center">
            <span className="text-[10px] uppercase tracking-wider text-slate-500">{term.category}</span>
            <Link
              to={`/glossary#${term.id}`}
              onClick={() => setOpen(false)}
              className="text-xs text-teal-400 hover:text-teal-300 hover:underline"
            >
              Learn more →
            </Link>
          </div>
        </div>
      )}
    </span>
  )
}
