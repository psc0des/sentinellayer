/**
 * Glossary.jsx — full searchable glossary page.
 *
 * Powered by the same dashboard/src/data/glossary.json that backs the
 * inline <InfoIcon> popovers, so adding or editing a term updates both
 * surfaces.
 *
 * Features:
 *   - Search box (matches term, short, long)
 *   - Category filter chips
 *   - Entries grouped by category, A–Z within each
 *   - Hash deep-linking: /glossary#escalated scrolls to and briefly
 *     highlights the matching entry
 *   - Related-term chips scroll to other entries within the page
 */

import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { BookOpen, Search } from 'lucide-react'
import GlowCard from '../components/magicui/GlowCard'
import glossary from '../data/glossary.json'

const CATEGORY_ORDER = ['verdict', 'agent', 'tier', 'score', 'playbook', 'concept']

const CATEGORY_LABEL = {
  verdict: 'Verdicts',
  agent: 'Agents',
  tier: 'Tiers',
  score: 'Scores',
  playbook: 'Playbook & Execution',
  concept: 'Concepts',
}

const CATEGORY_ACCENT = {
  verdict: 'text-rose-400 border-rose-500/30 bg-rose-500/10',
  agent: 'text-teal-400 border-teal-500/30 bg-teal-500/10',
  tier: 'text-indigo-400 border-indigo-500/30 bg-indigo-500/10',
  score: 'text-amber-400 border-amber-500/30 bg-amber-500/10',
  playbook: 'text-emerald-400 border-emerald-500/30 bg-emerald-500/10',
  concept: 'text-slate-300 border-slate-600/40 bg-slate-700/20',
}

const TERM_INDEX = Object.fromEntries(glossary.map((t) => [t.id, t]))

export default function Glossary() {
  const location = useLocation()
  const [query, setQuery] = useState('')
  const [activeCategory, setActiveCategory] = useState('all')
  const [highlightedId, setHighlightedId] = useState(null)
  const sectionRefs = useRef({})

  const normalizedQuery = query.trim().toLowerCase()

  const filtered = useMemo(() => {
    return glossary.filter((entry) => {
      if (activeCategory !== 'all' && entry.category !== activeCategory) return false
      if (!normalizedQuery) return true
      const haystack = `${entry.term} ${entry.short} ${entry.long}`.toLowerCase()
      return haystack.includes(normalizedQuery)
    })
  }, [activeCategory, normalizedQuery])

  const grouped = useMemo(() => {
    const buckets = {}
    for (const entry of filtered) {
      if (!buckets[entry.category]) buckets[entry.category] = []
      buckets[entry.category].push(entry)
    }
    for (const cat of Object.keys(buckets)) {
      buckets[cat].sort((a, b) => a.term.localeCompare(b.term))
    }
    return buckets
  }, [filtered])

  // Hash deep-link: scroll to and briefly highlight the matching entry.
  useEffect(() => {
    const hash = location.hash.replace(/^#/, '')
    if (!hash) return undefined
    const el = sectionRefs.current[hash]
    if (!el) return undefined
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    setHighlightedId(hash)
    const t = setTimeout(() => setHighlightedId(null), 1800)
    return () => clearTimeout(t)
  }, [location.hash, filtered])

  const totalCount = glossary.length
  const showingCount = filtered.length

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center gap-3 mb-2">
        <BookOpen className="w-6 h-6 text-teal-400" />
        <h1 className="text-2xl font-semibold text-slate-100">Glossary &amp; FAQ</h1>
      </div>
      <p className="text-sm text-slate-400 mb-6">
        Definitions for the verdicts, agents, scores, and concepts you see across the dashboard.
        Showing {showingCount} of {totalCount} terms.
      </p>

      {/* Search + filters */}
      <GlowCard className="p-4 mb-6">
        <div className="flex items-center gap-2 mb-3">
          <Search className="w-4 h-4 text-slate-500" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search terms, definitions, examples…"
            className="flex-1 bg-transparent text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none"
            aria-label="Search glossary"
          />
        </div>
        <div className="flex flex-wrap gap-2">
          <CategoryChip
            label={`All (${totalCount})`}
            active={activeCategory === 'all'}
            onClick={() => setActiveCategory('all')}
          />
          {CATEGORY_ORDER.map((cat) => {
            const count = glossary.filter((t) => t.category === cat).length
            return (
              <CategoryChip
                key={cat}
                label={`${CATEGORY_LABEL[cat]} (${count})`}
                accent={CATEGORY_ACCENT[cat]}
                active={activeCategory === cat}
                onClick={() => setActiveCategory(cat)}
              />
            )
          })}
        </div>
      </GlowCard>

      {/* Empty state */}
      {showingCount === 0 && (
        <div className="text-center py-12 text-slate-500 text-sm">
          No terms match your search. Try a different query or clear the filter.
        </div>
      )}

      {/* Grouped entries */}
      {CATEGORY_ORDER.map((cat) => {
        const entries = grouped[cat]
        if (!entries || entries.length === 0) return null
        return (
          <section key={cat} className="mb-8">
            <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500 mb-3">
              {CATEGORY_LABEL[cat]}
            </h2>
            <div className="space-y-3">
              {entries.map((entry) => (
                <article
                  key={entry.id}
                  id={entry.id}
                  ref={(el) => {
                    if (el) sectionRefs.current[entry.id] = el
                  }}
                  className={`rounded-lg border p-4 transition-colors ${
                    highlightedId === entry.id
                      ? 'border-teal-500/60 bg-teal-500/5'
                      : 'border-slate-800 bg-slate-900/40'
                  }`}
                >
                  <div className="flex items-start justify-between gap-3 mb-2">
                    <h3 className="text-base font-semibold text-slate-100">{entry.term}</h3>
                    <span
                      className={`text-[10px] font-medium uppercase tracking-wider px-2 py-0.5 rounded-full border ${CATEGORY_ACCENT[entry.category]}`}
                    >
                      {entry.category}
                    </span>
                  </div>
                  <p className="text-sm text-slate-300 leading-relaxed mb-3">{entry.long}</p>
                  {entry.related && entry.related.length > 0 && (
                    <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-slate-800">
                      <span className="text-[10px] uppercase tracking-wider text-slate-500">Related</span>
                      {entry.related.map((rid) => {
                        const r = TERM_INDEX[rid]
                        if (!r) return null
                        return (
                          <a
                            key={rid}
                            href={`#${rid}`}
                            className="text-xs px-2 py-0.5 rounded-md bg-slate-800 text-slate-300 hover:bg-slate-700 hover:text-slate-100 transition-colors"
                          >
                            {r.term}
                          </a>
                        )
                      })}
                    </div>
                  )}
                </article>
              ))}
            </div>
          </section>
        )
      })}
    </div>
  )
}

function CategoryChip({ label, accent, active, onClick }) {
  const base = 'text-xs px-3 py-1 rounded-full border transition-colors'
  if (active) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`${base} ${accent || 'text-slate-200 border-slate-500 bg-slate-700/40'}`}
      >
        {label}
      </button>
    )
  }
  return (
    <button
      type="button"
      onClick={onClick}
      className={`${base} text-slate-400 border-slate-700 bg-transparent hover:border-slate-600 hover:text-slate-200`}
    >
      {label}
    </button>
  )
}
