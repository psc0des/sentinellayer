/**
 * WorkflowDiagram — renders the governance workflow topology as a Mermaid SVG.
 *
 * Topology is immutable, so we fetch + render once per mount. The diagram
 * source comes from GET /api/workflow/diagram which delegates to
 * agent_framework.WorkflowViz.to_mermaid() on the backend.
 *
 * Mermaid is loaded dynamically so the ~700 KB library doesn't bloat the
 * initial bundle — it's only pulled in when this component actually mounts.
 */

import React, { useEffect, useRef, useState } from 'react'
import { fetchWorkflowDiagram } from '../api'
import GlowCard from './magicui/GlowCard'

const MERMAID_CONFIG = {
  startOnLoad: false,
  theme: 'dark',
  securityLevel: 'strict',
  themeVariables: {
    background: 'transparent',
    primaryColor: '#1e293b',
    primaryTextColor: '#e2e8f0',
    primaryBorderColor: '#475569',
    lineColor: '#64748b',
    secondaryColor: '#334155',
    tertiaryColor: '#0f172a',
  },
  flowchart: {
    htmlLabels: true,
    curve: 'basis',
    padding: 20,
  },
}

export default function WorkflowDiagram() {
  const containerRef = useRef(null)
  const [status, setStatus] = useState('loading') // loading | ready | error
  const [errorMsg, setErrorMsg] = useState('')

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const [{ default: mermaid }, payload] = await Promise.all([
          import('mermaid'),
          fetchWorkflowDiagram(),
        ])
        if (cancelled) return

        mermaid.initialize(MERMAID_CONFIG)
        const id = `wf-diagram-${Date.now()}`
        const { svg } = await mermaid.render(id, payload.diagram)
        if (cancelled) return

        if (containerRef.current) {
          containerRef.current.innerHTML = svg
        }
        setStatus('ready')
      } catch (err) {
        if (cancelled) return
        setErrorMsg(err.message || 'Failed to render diagram')
        setStatus('error')
      }
    }

    load()
    return () => { cancelled = true }
  }, [])

  return (
    <GlowCard color="purple" intensity="low">
      <div className="p-6">
        <div className="flex items-baseline justify-between mb-4">
          <h2 className="text-lg font-semibold text-slate-100">
            How RuriSkry Decides
          </h2>
          <span className="text-xs text-slate-400 font-mono">
            Phase 33C · WorkflowBuilder
          </span>
        </div>

        <p className="text-sm text-slate-400 mb-4 leading-relaxed">
          Every proposed action fans out from <code className="text-purple-300">dispatch</code> to four governance agents
          in parallel. Their results join at <code className="text-purple-300">scoring</code>, and
          {' '}<code className="text-purple-300">condition_gate</code> may promote APPROVED_IF → APPROVED when conditions
          are already satisfied.
        </p>

        {status === 'loading' && (
          <div className="h-64 flex items-center justify-center text-slate-500 text-sm">
            Loading workflow topology…
          </div>
        )}

        {status === 'error' && (
          <div className="h-64 flex items-center justify-center text-amber-300 text-sm">
            Could not render diagram: {errorMsg}
          </div>
        )}

        <div
          ref={containerRef}
          className="overflow-x-auto flex justify-center"
          style={{ display: status === 'ready' ? 'flex' : 'none' }}
        />
      </div>
    </GlowCard>
  )
}
