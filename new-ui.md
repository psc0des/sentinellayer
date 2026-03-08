# RuriSkry Dashboard — Enterprise UI Polish Pass

## Visual Target

I've generated mockup images showing the exact look & feel this dashboard should have. The current design has the right structure (sidebar, pages, scan history) but needs **visual craft** to feel enterprise-grade.

**Use these mockups as your design targets — don't copy any existing product (Datadog, Azure, Wiz). This is a UNIQUE design language.**

### Overview Page Target
![Overview mockup](C:\Users\THISPC\.gemini\antigravity\brain\3f26bb10-1e19-49a4-8fae-31e77a84c53d\overview_mockup_1772941510190.png)

### Decisions Page Target
![Decisions mockup](C:\Users\THISPC\.gemini\antigravity\brain\3f26bb10-1e19-49a4-8fae-31e77a84c53d\decisions_mockup_1772941553565.png)

---

## What's Wrong Right Now (specific CSS/design problems)

### 1. FLATNESS — Everything is the same depth
Every card, sidebar, table, and chart sits on identical `bg-slate-800` with `border-slate-700`. There are no visual layers.

**Fix — Add depth with these exact patterns:**
```css
/* Glass card effect — use on all cards and panels */
.glass-card {
  background: rgba(17, 24, 39, 0.7);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 12px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.3), inset 0 1px 0 rgba(255, 255, 255, 0.04);
}

/* Sidebar — slightly different tone from content area */
.sidebar {
  background: linear-gradient(180deg, rgba(13, 31, 45, 0.95) 0%, rgba(10, 15, 26, 0.98) 100%);
  backdrop-filter: blur(20px);
  border-right: 1px solid rgba(255, 255, 255, 0.04);
}

/* Page background — subtle gradient, NOT flat color */
.main-content {
  background: linear-gradient(135deg, #0a0f1a 0%, #0d1520 50%, #0a0f1a 100%);
}
```

### 2. METRIC CARDS — No personality
Current: plain number + label on dark background. Boring.

**Fix — Add accent edge glow + sparkline:**
```css
.metric-card {
  position: relative;
  overflow: hidden;
}

/* Colored left-edge accent glow */
.metric-card::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  border-radius: 3px;
}
.metric-card.teal::before { background: #14b8a6; box-shadow: 0 0 12px rgba(20,184,166,0.4); }
.metric-card.green::before { background: #22c55e; box-shadow: 0 0 12px rgba(34,197,94,0.4); }
.metric-card.amber::before { background: #f59e0b; box-shadow: 0 0 12px rgba(245,158,11,0.4); }
.metric-card.red::before   { background: #ef4444; box-shadow: 0 0 12px rgba(239,68,68,0.3); }

/* Number styling */
.metric-number {
  font-size: 2rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  background: linear-gradient(135deg, #ffffff 0%, #94a3b8 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

/* Hover lift */
.metric-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  transition: all 0.2s ease;
}
```

### 3. VERDICT BADGES — Just colored text, not badge pills
Current: "ESCALATED" is just orange text. Zero visual weight.

**Fix — Filled pill badges with glow:**
```css
.badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 10px;
  border-radius: 9999px;
  font-size: 0.7rem;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.badge-approved {
  background: rgba(34, 197, 94, 0.15);
  color: #4ade80;
  border: 1px solid rgba(34, 197, 94, 0.25);
}
.badge-escalated {
  background: rgba(245, 158, 11, 0.15);
  color: #fbbf24;
  border: 1px solid rgba(245, 158, 11, 0.25);
  box-shadow: 0 0 8px rgba(245, 158, 11, 0.1);
}
.badge-denied {
  background: rgba(239, 68, 68, 0.15);
  color: #f87171;
  border: 1px solid rgba(239, 68, 68, 0.25);
  box-shadow: 0 0 8px rgba(239, 68, 68, 0.1);
}
```

### 4. TABLE — Rows are lifeless

**Fix — Interactive row styling:**
```css
.table-row {
  transition: all 0.15s ease;
  border-left: 3px solid transparent;
}
.table-row:hover {
  background: rgba(20, 184, 166, 0.04);
  border-left-color: rgba(20, 184, 166, 0.5);
  cursor: pointer;
}

/* Subtle alternating rows */
.table-row:nth-child(even) {
  background: rgba(255, 255, 255, 0.01);
}

/* Table header */
.table-header {
  background: rgba(255, 255, 255, 0.03);
  font-size: 0.7rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #64748b;
}
```

### 5. SRI TREND CHART — Default recharts look

**Fix — Add gradient fill and threshold bands:**
```jsx
{/* In the recharts Area component */}
<defs>
  <linearGradient id="sriGradient" x1="0" y1="0" x2="0" y2="1">
    <stop offset="5%" stopColor="#14b8a6" stopOpacity={0.3}/>
    <stop offset="95%" stopColor="#14b8a6" stopOpacity={0}/>
  </linearGradient>
</defs>
<Area
  type="monotone"
  dataKey="sri"
  stroke="#14b8a6"
  strokeWidth={2}
  fill="url(#sriGradient)"
/>

{/* Add threshold reference lines */}
<ReferenceLine y={25} stroke="rgba(34,197,94,0.2)" strokeDasharray="3 3" />
<ReferenceLine y={60} stroke="rgba(239,68,68,0.2)" strokeDasharray="3 3" />
```

### 6. SIDEBAR — Needs polish

**Fix — Active state glow + hover effects:**
```css
.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border-radius: 8px;
  color: #64748b;
  transition: all 0.15s ease;
  border-left: 3px solid transparent;
  margin: 2px 8px;
}
.nav-item:hover {
  background: rgba(20, 184, 166, 0.06);
  color: #cbd5e1;
}
.nav-item.active {
  background: rgba(20, 184, 166, 0.1);
  color: #14b8a6;
  border-left-color: #14b8a6;
  box-shadow: inset 0 0 20px rgba(20, 184, 166, 0.05);
}
```

### 7. MICRO-ANIMATIONS — Add life

```css
/* Numbers count-up animation on load */
@keyframes fadeInUp {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
.metric-card { animation: fadeInUp 0.4s ease forwards; }
.metric-card:nth-child(2) { animation-delay: 0.05s; }
.metric-card:nth-child(3) { animation-delay: 0.1s; }
.metric-card:nth-child(4) { animation-delay: 0.15s; }

/* New verdict pulse */
@keyframes pulse-badge {
  0% { box-shadow: 0 0 0 0 rgba(245,158,11,0.4); }
  70% { box-shadow: 0 0 0 6px rgba(245,158,11,0); }
  100% { box-shadow: 0 0 0 0 rgba(245,158,11,0); }
}
.badge-new { animation: pulse-badge 2s ease-in-out 3; }

/* Page transitions */
.page-content {
  animation: fadeInUp 0.2s ease;
}
```

### 8. SCAN BUTTON ICONS — Replace emojis

Replace 💰 🤖 🔒 in the scan cards with Lucide React SVG icons:
```jsx
import { DollarSign, Activity, Shield } from 'lucide-react';

// Instead of 💰 Cost Scan:
<DollarSign size={18} className="text-teal-400" />

// Instead of 📡 SRE Scan:
<Activity size={18} className="text-blue-400" />

// Instead of 🔒 Deploy Scan:
<Shield size={18} className="text-amber-400" />
```

---

## Color Palette (UNIQUE to RuriSkry — not copied from any product)

| Token | Hex | Usage |
|:------|:----|:------|
| `--bg-base` | `#0a0f1a` | Page background |
| `--bg-sidebar` | `#0d1f2d` | Sidebar gradient start |
| `--bg-card` | `rgba(17,24,39,0.7)` | Glass card backgrounds |
| `--border-subtle` | `rgba(255,255,255,0.06)` | Card/table borders |
| `--accent-primary` | `#14b8a6` | Teal — primary accent, active states |
| `--accent-success` | `#22c55e` | Green — approved, clean |
| `--accent-warning` | `#f59e0b` | Amber — escalated, review needed |
| `--accent-danger` | `#ef4444` | Red — denied, blocked |
| `--text-primary` | `#f1f5f9` | Primary text |
| `--text-secondary` | `#94a3b8` | Labels, descriptions |
| `--text-muted` | `#475569` | Timestamps, metadata |

---

## Summary of Changes

This is NOT a layout restructure — the pages, components, and routing are already correct. This is a **visual polish pass**:

1. Apply `.glass-card` to every card and panel
2. Add colored left-edge glows to metric cards
3. Replace verdict text with filled pill badges
4. Add row hover effects + alternating backgrounds to all tables
5. Add gradient fill + threshold bands to the SRI chart
6. Polish sidebar with hover/active states and glass effect
7. Add `fadeInUp` animations on page load
8. Replace remaining emojis with Lucide SVG icons
9. Apply the colour palette consistently

DO NOT change any backend API calls, routing, or data flow. Only CSS and JSX rendering changes.
