# RuriSkry Dashboard — QA Report

**Tested by**: AI QA Agent · **Date**: Mar 8, 2026  
**Test scope**: Full E2E — all 3 agent scans, every dashboard panel, drilldown, HITL flow  
**Dashboard URL**: `http://localhost:5173/` · **API**: `http://localhost:8000/api`

---

## 1. Test Summary

| Agent | Proposals | Verdicts | Outcome |
|:------|:----------|:---------|:--------|
| Cost | 0 | 0 | Clean — no idle/over-provisioned resources found |
| SRE (Monitoring) | 0 | 0 | Clean — no anomalies found |
| Deploy | 1 | 1 | ⚠️ ESCALATED · SSH rule exposes port 22 to `*` (SRI 37.8) |

**HITL tested**: Clicked "Decline / Ignore" on the ESCALATED verdict → transitioned to `Dismissed` ✅

---

## 2. All Issues Found

### 🔴 Functional Bugs (6)

---

#### BUG-01 · Reset Doesn't Wipe Cosmos DB | P1

**What**: Clicked 🗑 Reset → refreshed page → **61 evaluations still visible**. Agent cards still show `deploy-agent: 45 proposed, 28 approved`. The reset only deletes local JSON files in `data/decisions/`, `data/executions/`, `data/scans/`.

**Root cause**: `POST /api/admin/reset` in [dashboard_api.py](file:///e:/AI/Hackathon/sentinellayer/src/api/dashboard_api.py) calls `os.remove()` on local files and clears in-memory `_scans` dict. But `DecisionTracker`, `AgentRegistry`, and `ScanRunTracker` read from **Cosmos DB** in live mode — which is never wiped.

**Fix**: Add a `reset_cosmos` flag. When true, delete all documents from the 3 Cosmos containers. Or add a `scope` param: `local` (current behaviour) vs `full` (includes Cosmos).

---

#### BUG-02 · "Last Run" Says "No Results" After Clean Scan | P1

**What**: Ran Cost Scan → completed with 0 verdicts → ⋮ → Last Run Results → **"No results found. Run a scan first."** Meanwhile the API returns `status: "complete", evaluations_count: 0`.

**Root cause**: [ConnectedAgents.jsx line 138](file:///e:/AI/Hackathon/sentinellayer/dashboard/src/components/ConnectedAgents.jsx#L138) — `LastRunPanel` checks `evaluations.length === 0` and shows the empty message. It treats "0 verdicts" as "never ran".

```jsx
// Current (broken)
{evaluations.length === 0 ? (
  <p>No results found. Run a scan first.</p>
) : ( ... )}
```

**Fix**: Check `data.status === "complete"` separately. When `evaluations.length === 0 && data.status === "complete"`, show: "✅ Scan completed — no issues found" with timestamp and scan ID.

````carousel
![API returns valid complete scan](C:\Users\THISPC\.gemini\antigravity\brain\3f26bb10-1e19-49a4-8fae-31e77a84c53d\last_run_json_1772911805780.png)
<!-- slide -->
![Frontend ignores it and shows "no results"](C:\Users\THISPC\.gemini\antigravity\brain\3f26bb10-1e19-49a4-8fae-31e77a84c53d\cost_agent_last_run_modal_1772912571207.png)
````

---

#### BUG-03 · "Done · 0 verdicts" Not Clickable | P2

**What**: After cost/SRE scans complete, AgentControls shows grey text `Done · 0 verdicts (no issues found)`. This is **not a link**. Compare: deploy shows green clickable `Done · 1 verdict(s) →`.

**Root cause**: [AgentControls.jsx lines 90–104](file:///e:/AI/Hackathon/sentinellayer/dashboard/src/components/AgentControls.jsx#L90-L104). When `evaluations_count > 0`, renders a clickable `<span>` with `onClick`. When `=== 0`, renders a plain `<span>` with no handler.

**Fix**: Make the 0-verdict text clickable too. Link to a scan summary view or reopen the Live Log replay.

---

#### BUG-04 · Back Button Unreliable in Drilldown | P3

**What**: After viewing an Evaluation Drilldown, clicking "← Back to Dashboard" sometimes doesn't navigate. The drilldown stays visible. Required clicking the RuriSkry logo or full page refresh.

**Root cause**: Likely React state — `selectedEvaluation` in `App.jsx` isn't reliably cleared. Possibly a stale closure or event propagation issue in the back button's `onClick`.

**Fix**: Verify `setSelectedEvaluation(null)` fires correctly. Add a `key` prop or use `useCallback` to prevent stale closures.

---

#### BUG-05 · Scan Status Lost on Page Reload | P3

**What**: After scans complete, buttons show "Done · 1 verdict(s) →". Refreshing the page resets all buttons to default — the "Done" badges disappear.

**Root cause**: `lastStatus` in [AgentControls.jsx line 131](file:///e:/AI/Hackathon/sentinellayer/dashboard/src/components/AgentControls.jsx#L131) is `useState({})` — not fetched from the API on mount.

**Fix**: On mount, call `GET /api/agents/{name}/last-run` for each agent type and populate `lastStatus` from the durable scan tracker.

---

#### BUG-06 · Agent Card Ordering Changes After Scan | P3

**What**: Before scans, the card order was `cost → deploy → monitoring`. After scans completed, order changed to `deploy → monitoring → cost`. The sort is by `last_seen` (most recent first), but this reordering is disorienting.

**Root cause**: `GET /api/agents` sorts by `last_seen` descending. As each agent finishes, its `last_seen` updates, changing the sort order.

**Fix**: Either (a) sort alphabetically by default, or (b) add a sort toggle (by name / by last seen / by actions proposed), or (c) pin card positions.

---

### 🟡 UX — Information Gaps (6)

---

#### UX-01 · Clean Scans Have Zero Audit Trail | P2

**What**: After cost and SRE scans finished with 0 verdicts — **no new row** in Recent Decisions, **no entry** in Live Activity Feed, **no metric change** (61 stayed 61). The only evidence a scan ran is the `last_seen` timestamp changing on the agent card.

**Impact**: In a well-managed environment (healthy infra, few issues), the dashboard always looks dead. An auditor asking "prove you're running governance scans" has no evidence.

**Fix**: Add scan lifecycle events to the dashboard. Options: (a) a "Scan History" panel, (b) a "Recent Scans" section separate from "Recent Decisions", or (c) mark 0-verdict scans as "health check passed" rows in the decision table.

---

#### UX-02 · History Panel Shows Only Past Verdicts | P2

**What**: ⋮ → History for cost-optimization-agent showed old verdicts from previous sessions (Feb 28, Mar 3, etc.). No trace of the scan that **just completed**. For monitoring-agent (which has 0 verdicts ever from scans), History shows records from demo runs only.

**Impact**: "When was the last scan?" is unanswerable. "Which resources were checked?" is unknown.

**Fix**: Merge scan-run records into the History modal. Show entries like "Mar 8, 01:08 AM — Scan completed, 0 issues found (12 resources checked, 23s)."

---

#### UX-03 · No Scan Run History Concept | P2

**What**: The backend has rich scan data (`ScanRunTracker` stores scan_id, agent_type, started_at, completed_at, proposals_count, evaluations_count) — but **no dashboard component surfaces it**. There's no view that shows:

| Scan | Agent | Started | Duration | Resources | Proposals | Verdicts |
|:-----|:------|:--------|:---------|:----------|:----------|:---------|
| `cbc383..` | cost | 01:08 AM | 23s | whole sub | 0 | 0 |
| `7cb5a4..` | monitoring | 01:09 AM | 63s | whole sub | 0 | 0 |
| `659108..` | deploy | 01:10 AM | 111s | whole sub | 1 | 1 |

**Impact**: Can't answer: scan frequency, agent duration trends, coverage tracking, or "which scan found this issue?"

**Fix**: Add a "Scan Runs" table/panel that queries the `ScanRunTracker` data.

---

#### UX-04 · No Scan-to-Verdict Linkage in Decision Table | P3

**What**: The Recent Decisions table shows `resource | action | SRI | verdict | time` but doesn't indicate **which scan produced it**. When multiple scans run per day on the same resource, entries are indistinguishable.

**Fix**: Add a subtle `scan 659108..` annotation or a "scan run" column.

---

#### UX-05 · Agent Details Panel Is Sparse | P3

**What**: ⋮ → Agent Details shows: name, registered time, last seen, totals (proposed/approved/escalated/denied), agent card URL. That's just 8 rows of key-value pairs. There's no: scan history, resource coverage, trending scores, failure log, or configuration.

**Fix**: Enrich with: last 5 scan summaries, avg scan duration, top resources scanned, success rate trend.

---

#### UX-06 · No Confirmation Dialog for Reset | P3

**What**: Clicking 🗑 Reset immediately fires without any "Are you sure?" confirmation. One misclick wipes all local data.

**Fix**: Add a modal: "This will delete all local decisions, executions, and scans. Continue?"

---

### 🔵 Design / Polish (6)

---

#### DESIGN-01 · Metrics Bar Doesn't Update Live | P3

**What**: After running scans, the metrics bar (Total Evaluations, Approval Rate, etc.) needs a manual refresh or ~5s auto-poll to update. During scans, the numbers are stale while the user watches the Live Log showing new verdicts.

**Fix**: Trigger an immediate re-fetch of `/api/metrics` when any scan status transitions to `complete`.

---

#### DESIGN-02 · Decision Table Has No Pagination or Filtering | P3

**What**: The Recent Decisions table shows up to 20 rows. With 62 evaluations, most are hidden. There's no pagination, no filter by agent/verdict/date, no search.

**Fix**: Add pagination controls. Add filter dropdowns: agent type, verdict type, date range. Add a resource search box.

---

#### DESIGN-03 · Live Activity Feed Duplicates Decision Table | P2

**What**: At the bottom of the page, the Live Activity Feed shows essentially the same data as Recent Decisions but in a different format (timestamp + "ssh escalated via deploy 40m ago"). Both are clickable into the same drilldown. There's no clear distinction in purpose.

**Fix**: Either (a) differentiate them — make the Feed truly real-time (WebSocket/SSE) showing events as they happen and the Table be a searchable historical view, or (b) merge them into one unified view.

---

#### DESIGN-04 · SRI Gauge Uses Same Resource Across Refreshes | P3

**What**: The "Latest Evaluation" SRI gauge always shows the most recent verdict. If the last verdict was from deploy-agent on `ssh`, the gauge permanently shows "ssh / modify nsg / via deploy". It would be more useful if users could select which evaluation to display, or if it rotated through recent evaluations.

---

#### DESIGN-05 · 3-Dot Menu Icons Are Emoji, Not Icons | P3

**What**: The ⋮ dropdown menu uses emoji characters (▶ ⏹ 📋 📊 📄 ℹ️) instead of proper SVG icons. These render inconsistently across platforms and look informal for an enterprise product.

**Fix**: Replace with consistent SVG icon set (e.g., Heroicons, Lucide, or Phosphor).

---

#### DESIGN-06 · No Loading Skeleton or Empty State Designs | P3

**What**: When the page loads, components briefly show nothing before data arrives (no skeleton loaders). Empty states (e.g., 0 agents, 0 evaluations) show minimal text with no illustration or call-to-action.

**Fix**: Add skeleton loaders during API fetch. Design proper empty states with illustrations and actionable guidance.

---

## 3. Honest Dashboard UI Assessment

### What it does well

The **Live Scan Log** is genuinely excellent — the merged 3-agent view with colour-coded badges, timestamped events, and progressive reasoning is something enterprise competitors don't have. The **Evaluation Drilldown** is comprehensive: SRI dimensional bars, counterfactual analysis, agent reasoning, and the HITL panel are enterprise-grade features. The **HITL flow** (verdict → awaiting_review → 4 buttons → dismissed) works end-to-end and is well-designed.

### Where it falls short of enterprise quality

> [!IMPORTANT]
> The core issue: **this dashboard is built for someone who already understands RuriSkry's architecture**. An enterprise user who needs to prove governance compliance, audit scan history, or troubleshoot agent behaviour will struggle.

**1. Single-page layout without navigation**

Everything is crammed onto one scrolling page. Enterprise dashboards (Azure Portal, Datadog, PagerDuty, Wiz) use **left sidebar navigation** with distinct pages: Overview, Scans, Agents, Policies, Audit Log, Settings. RuriSkry mixes agent controls, metrics, decisions, and a live feed on one page with no hierarchy.

**2. Information architecture is verdict-centric, not operations-centric**

The dashboard answers "what verdicts were issued?" but not "is my environment healthy?", "are my agents running?", "what did the last scan cover?", "are there unresolved issues?" An enterprise user's workflow is: check health → run scan → review findings → take action → verify resolution. The dashboard doesn't support this journey.

**3. No data density controls**

There's no date range picker, no filters (by agent, verdict type, resource group, severity), no search, no pagination, no sorting on columns. With 62 records already, the table is overflowing. At enterprise scale (thousands of evaluations), this will be unusable.

**4. Typography and spacing**

The ALL-CAPS section headers (CONNECTED AGENTS, AGENT CONTROLS, TOTAL EVALUATIONS, RECENT DECISIONS) feel aggressive. Enterprise dashboards use sentence case or title case with more whitespace. The font sizes jump from `text-xs` (10px) to `text-2xl` (24px) with nothing in between for readable body text.

**5. Colour system lacks hierarchy**

The dark slate theme is attractive but monochromatic. There's no distinct visual hierarchy between: the action area (scan buttons), the summary area (metrics), the data area (tables), and the detail area (drilldown). Everything is `bg-slate-800` with `border-slate-700`. Enterprise dashboards use subtle background variations, dividers, and shadow depths to create visual zones.

**6. Missing features that enterprise users expect**

| Missing | Why it matters |
|:--------|:--------------|
| Role-based access control | No login — anyone with the URL has full admin access |
| Export / download reports | Compliance teams need PDFs/CSVs of audit trails |
| Date range selector | "Show me last week's scans" is impossible |
| Resource group filter on dashboard (not just scan input) | Can't scope the view to one RG |
| Notification preferences | Can't configure per-user/per-team alert rules |
| Scheduled scans | Must manually trigger every scan |
| Dashboard sharing / embedding | Can't embed in Grafana, Azure Dashboard, or Teams |
| Trend charts / time-series | No "SRI over time", "scans per day", or "issues resolved" graphs |

### Overall assessment

The dashboard is a **strong hackathon prototype** with some genuinely impressive features (Live Scan Log, Drilldown with counterfactuals, HITL execution flow). The backend architecture is enterprise-capable. But the frontend falls into the common trap of being **a developer tool disguised as a dashboard** — it shows system internals rather than answering the questions an operations team would ask.

For enterprise readiness, the priorities are:
1. **Navigation + multi-page layout** — separate Overview, Scans, Agents, Policies, Audit
2. **Scan history as a first-class concept** — not just verdicts
3. **Filtering, search, pagination** — on every data table
4. **Empty state design** — "0 verdicts" should feel like a success, not a bug

---

## 4. Recommended Fix Priority

### Sprint 1 — Must-fix (affects trust + core functionality)
- BUG-01: Reset should wipe Cosmos or clearly scope the reset
- BUG-02: Last Run panel shows "scan complete, 0 issues" instead of "no results"
- UX-01: Add any trace of clean scans to the UI
- UX-03: Add a basic Scan History view

### Sprint 2 — Important (enterprise readiness)
- BUG-03: Make 0-verdict status clickable
- UX-02: Merge scan-run info into History panel
- DESIGN-02: Pagination + filtering on decision table
- DESIGN-03: Differentiate Live Feed vs Decision Table

### Sprint 3 — Polish
- BUG-04/05/06: Back button, scan status persistence, card ordering
- UX-04/05/06: Scan-verdict linkage, richer Agent Details, reset confirmation
- DESIGN-01/04/05/06: Live metrics, gauge improvements, SVG icons, loading skeletons
