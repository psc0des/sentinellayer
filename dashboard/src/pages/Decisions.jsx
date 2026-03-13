/**
 * Decisions.jsx — searchable, filterable, sortable governance decision table
 * with inline drilldown view.
 *
 * URL query: ?exec=<execution_id> scrolls to and highlights a specific pending
 * review. The drilldown replaces the table when a row is clicked.
 */

import React, { useState, useEffect } from 'react'
import { useOutletContext, useNavigate, useLocation } from 'react-router-dom'
import EvaluationDrilldown from '../components/EvaluationDrilldown'
import DecisionTable from '../components/DecisionTable'
import { ChevronRight } from 'lucide-react'

export default function Decisions() {
  const { evaluations, pendingReviews, fetchAll } = useOutletContext()
  const [drilldownEval, setDrilldownEval] = useState(null)
  const navigate  = useNavigate()
  const location  = useLocation()

  // Read ?agent= and ?exec= from URL
  const params       = new URLSearchParams(location.search)
  const agentParam   = params.get('agent') ?? 'all'

  // If the URL contains ?exec=..., pre-open the relevant drilldown via the
  // execution_id stored in pendingReviews (which have verdict_snapshot)
  useEffect(() => {
    const p = new URLSearchParams(location.search)
    const execId = p.get('exec')
    if (!execId || !pendingReviews.length) return

    const review = pendingReviews.find(r => r.execution_id === execId)
    if (review?.verdict_snapshot) {
      setDrilldownEval(review.verdict_snapshot)
    }
  }, [location.search, pendingReviews])

  function handleBack() {
    setDrilldownEval(null)
    // Remove query params when going back
    if (location.search) navigate('/decisions', { replace: true })
  }

  return (
    <div className="p-6 max-w-6xl mx-auto">

      {drilldownEval ? (
        <>
          {/* Breadcrumb */}
          <nav className="flex items-center gap-1.5 text-xs text-slate-500 mb-5">
            <button onClick={handleBack} className="hover:text-slate-300 transition-colors">
              Decisions
            </button>
            <ChevronRight className="w-3 h-3" />
            <span className="text-slate-300 font-medium truncate max-w-xs">
              {drilldownEval.resource_id?.split('/').filter(Boolean).pop() ?? 'Detail'}
              {' '}
              <span className={
                drilldownEval.decision?.toLowerCase() === 'approved'  ? 'text-emerald-400' :
                drilldownEval.decision?.toLowerCase() === 'escalated' ? 'text-amber-400' :
                                                                        'text-rose-400'
              }>
                ({(drilldownEval.decision ?? '').toUpperCase()})
              </span>
            </span>
          </nav>

          <EvaluationDrilldown
            evaluation={drilldownEval}
            onBack={handleBack}
          />
        </>
      ) : (
        <>
          <div className="mb-6">
            <h1 className="text-xl font-bold text-white">Decisions</h1>
            <p className="text-sm text-slate-500 mt-1">
              Full history of governance verdicts — click any row to drill down
            </p>
          </div>

          <DecisionTable
            key={agentParam}
            evaluations={evaluations}
            onSelect={setDrilldownEval}
            onRefresh={fetchAll}
            initialAgent={agentParam}
          />
        </>
      )}
    </div>
  )
}
