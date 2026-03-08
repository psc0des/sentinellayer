/**
 * Agents.jsx — connected agent cards and management.
 */

import React from 'react'
import { useOutletContext } from 'react-router-dom'
import ConnectedAgents from '../components/ConnectedAgents'

export default function Agents() {
  const { agents } = useOutletContext()

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      <div>
        <h1 className="text-xl font-bold text-white">Agents</h1>
        <p className="text-sm text-slate-500 mt-1">
          Operational agents connected via the A2A protocol
        </p>
      </div>

      <ConnectedAgents agents={agents} />
    </div>
  )
}
