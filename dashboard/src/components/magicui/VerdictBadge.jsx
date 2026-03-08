/**
 * VerdictBadge — high-contrast, distinctive verdict labels with glow.
 *
 * Used across DecisionTable, ConnectedAgents, and modals.
 *
 * Props:
 *   verdict — 'approved' | 'escalated' | 'denied' | any string
 *   size    — 'sm' | 'md' (default 'sm')
 *   glow    — enable subtle text-shadow glow (default true)
 */

export default function VerdictBadge({ verdict, size = 'sm', glow = true }) {
  const v = (verdict ?? '').toLowerCase()

  const configs = {
    approved: {
      bg: 'bg-emerald-500/10',
      border: 'border-emerald-500/40',
      text: 'text-emerald-300',
      dot: 'bg-emerald-400',
      shadow: glow ? '0 0 8px rgba(52,211,153,0.4)' : 'none',
      label: 'Approved',
    },
    escalated: {
      bg: 'bg-amber-500/10',
      border: 'border-amber-500/40',
      text: 'text-amber-300',
      dot: 'bg-amber-400',
      shadow: glow ? '0 0 8px rgba(251,191,36,0.4)' : 'none',
      label: 'Escalated',
    },
    denied: {
      bg: 'bg-rose-500/10',
      border: 'border-rose-500/40',
      text: 'text-rose-300',
      dot: 'bg-rose-400',
      shadow: glow ? '0 0 8px rgba(251,113,133,0.4)' : 'none',
      label: 'Denied',
    },
  }

  const cfg = configs[v]

  // Unknown verdict — neutral style
  if (!cfg) {
    return (
      <span className="px-2 py-0.5 rounded-full text-xs font-semibold uppercase bg-slate-700/60 text-slate-400 border border-slate-600">
        {v || '—'}
      </span>
    )
  }

  const padding = size === 'md' ? 'px-3 py-1' : 'px-2 py-0.5'
  const textSize = size === 'md' ? 'text-xs' : 'text-[11px]'

  return (
    <span
      className={`
        inline-flex items-center gap-1.5
        ${padding} rounded-full ${textSize} font-bold uppercase tracking-wide
        ${cfg.bg} ${cfg.border} ${cfg.text}
        border
      `}
      style={{ textShadow: cfg.shadow }}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${cfg.dot} shrink-0`} />
      {cfg.label}
    </span>
  )
}
