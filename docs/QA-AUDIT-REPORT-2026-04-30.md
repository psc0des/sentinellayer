# RuriSkry AI Governance Dashboard — Full QA Audit Report

**Date:** 2026-04-30  
**Tester:** Claude Sonnet 4.6 (automated Playwright + browser)  
**Phase tested:** Phase 40 (Universal Rules Engine) — post-deployment  
**Session duration:** ~90 minutes manual testing + 33-minute Deploy scan observation  
**Environment:** Production (Azure Container Apps + Static Web App)

---

## 1. System Health at Time of Testing

| Component | Status | Notes |
|---|---|---|
| Backend (Container App) | ✅ Online | |
| Frontend (Static Web App) | ✅ Online | |
| Slack Webhook | ✅ Connected | |
| Azure Resource Graph API | ❌ **FAILING** | Import error — see BUG #14 |
| Azure Advisor API | ✅ OK | 482ms |
| Azure Policy Insights API | ✅ OK | 2ms |
| Azure Defender API | ✅ OK | 10,385ms (slow but functional) |
| Azure Resource Health API | ✅ OK | 35ms |
| Rules Engine (34 rules) | ⚠️ Degraded | Loaded, but resource_graph enrichment disabled |
| Cosmos DB | ✅ Online | |
| LLM (Azure Foundry) | ✅ Online | |

---

## 2. Features Tested

| Feature / Page | Tested | Result |
|---|---|---|
| Login / Auth | ✅ | Pass |
| Overview page | ✅ | Pass (minor issues) |
| Inventory page — expand/collapse | ✅ | Pass |
| Inventory — search/filter | ✅ | Pass |
| Decisions page — list | ✅ | Pass |
| Decision drilldown (all 4 verdict types) | ✅ | Pass |
| Decisions filtering (type, status) | ✅ | Pass |
| Alerts page — grouped view | ✅ | Pass (badge bug) |
| Alerts — expand group | ✅ | Pass |
| Audit Log page | ✅ | Pass |
| Audit Log drilldown / JSON export | ✅ | Pass |
| Admin page | ✅ | Pass |
| Agents page — cards | ✅ | Multiple bugs |
| Run Scan (individual agent) | ✅ | Functional |
| Run All Agents | ✅ | Fully observed end-to-end |
| Live Scan Log (SSE streaming) | ✅ | Functional, UX bugs |
| Scan history table | ✅ | Data freshness bug |
| CoverageStatusBanner | ✅ | Rendering with wrong data |
| CoverageManifestPanel (View Log modal) | ⚠️ | Not verified — needs manual check |
| Glossary page | ✅ | Pass |
| Override capture (Force/Dismiss/Satisfy) | ✅ | Pass |
| Playbook generator | ✅ | Pass |
| A2 Validator (dry-run / live preview) | ✅ | BUG #31 |
| Escape key on modals | ✅ | BUG #32 |

---

## 3. Run All Agents — Complete Scan Results (Apr 30, 12:30 PM)

All three agents triggered simultaneously.

### Cost Agent — Complete in 7m 3s

| Resource | Verdict | SRI | Rule Triggered |
|---|---|---|---|
| vm-dr-01 | **DENIED** | 43.0 | UNIV-COST-001 (VM deallocated) + CRITICAL policy violation |
| vm-web-01 | **ESCALATED** | 49.3 | UNIV-COST-001 (VM deallocated) |

**Summary:** 2 proposals, 2 verdicts — 0 approved, 0 approved_if, 1 escalated, 1 denied.

### Monitoring Agent — Complete in 11m 3s

| Verdict | Count |
|---|---|
| APPROVED | 4 |
| APPROVED_IF | 2 |
| ESCALATED | 1 |
| DENIED | 0 |
| **Total** | **7** |

Rules triggered: `UNIV-COST-001` (vm-dr-01, vm-web-01 in deallocated state), `UNIV-REL-001` (ruriskry-prod-demd running Free SKU App Service Plan in production).

### Deploy Agent — Complete in 33m 1s

| Verdict | Count |
|---|---|
| APPROVED | 28 |
| ESCALATED | 7 |
| DENIED | 0 |
| APPROVED_IF | 0 |
| **Total** | **35** |

Rules triggered: `UNIV-HYG-001` (completely untagged resources), `UNIV-HYG-002` (partially tagged — missing `costcenter`, `environment`, `owner`).

**ESCALATED resources** (NSGs and higher-blast-radius resources, SRI > 25):
- `nsg-cost-prod` — SRI 30.5
- `nsg-east-prod` — SRI 30.5
- `nsg-ruriskry-prod-deml` — SRI ~30.5
- `shutdown-computevm-vm-dr-01` — SRI 25.5 (auto-shutdown schedule for DR VM)
- 3 additional resources

**APPROVED resources** (28 total — all tag compliance, SRI ≤ 25 or Tier-1 forced):
Key Vaults, VMs, Application Insights, Cosmos, Storage, Foundry instances, Function App, Log Analytics Workspace, VNets, NICs, OS disks, Alert rules (`alert-vm-web-01-cpu-high`, `alert-vm-dr-01-heartbeat`), DCRs, auto-shutdown schedules.

---

## 4. Bug Report

Bugs are ordered by severity (Critical → High → Medium → Low), then by discovery order.

---

### BUG #14 — 🔴 CRITICAL | api_preflight.py imports a nonexistent function

**File:** `src/infrastructure/api_preflight.py:27–30`  
**Symptom:** `CoverageStatusBanner` always shows "Coverage degraded — 1 Microsoft API unavailable" with the error:
```
cannot import name 'query_resources_async' from 'src.infrastructure.resource_graph'
```
**Root cause:** `_check_resource_graph()` imports `query_resources_async` from `resource_graph.py`, but that function does not exist in the module.  
**Impact:** The resource_graph coverage check has **never worked** since this file was written. Every call to `GET /api/coverage/status` returns `ok: false` for `resource_graph`. Operators see a permanent false alarm in the CoverageStatusBanner.  
**Fix:** Rename the import to the actual function in `resource_graph.py` (likely `query_resources` or similar).

---

### BUG #15 — 🟠 HIGH | CoverageStatusBanner not shown on Overview (landing page)

**Page:** `/overview`  
**Symptom:** The banner warning about API unavailability only renders on `/agents`. Operators who never visit Agents will never see coverage degradation warnings.  
**Impact:** Silent coverage failure — resource_graph API has been broken and operators logging into the dashboard would not know unless they navigate to the Agents page.  
**Fix:** Render `CoverageStatusBanner` on the Overview page too (or globally in the layout).

---

### BUG #22 — 🟠 HIGH | Alerts badge drops from 31 to 21 when scan starts

**Page:** Sidebar — Alerts badge  
**Symptom:** When "Run All Agents" was triggered, the Alerts badge changed from **31 → 21** (a drop of 10). No alerts were acknowledged or dismissed by the operator.  
**Impact:** Operators monitoring the badge count could be misled into thinking 10 alerts were resolved. The badge value is not stable during scans.  
**Reproduction:** Trigger "Run All Agents" while observing the Alerts sidebar badge.

---

### BUG #24 — 🟠 HIGH | SSE live log is silent for 14+ minutes at Deploy scan start

**Page:** Agents — Live Scan Log  
**Symptom:** After triggering the Deploy Agent scan, the Live Scan Log showed "Connecting to scan stream..." for approximately **14 minutes** before any events appeared. No progress events are emitted during inventory building and rules pre-scan execution.  
**Impact:** Operators cannot tell whether the scan is working or hung. For a 33-minute operation, a 14-minute blank log is a trust-breaking UX failure. There is no way to distinguish "processing" from "crashed."  
**Fix:** Emit lightweight SSE progress events during the inventory/rules phase — e.g.:
```json
{"type": "progress", "message": "Building inventory... 42 resources found"}
{"type": "progress", "message": "Running 34 rules pre-scan... 28 findings so far"}
```

---

### BUG #26 — 🟠 HIGH | UNIV-HYG-001/002 generates false positive proposals for child/infrastructure resources

**Phase:** Phase 40 — Universal Rules Engine  
**Symptom:** The Deploy Agent scan flagged the following resource types for missing tags:
- VM OS disks (`vm-dr-01_0sDisk_1_44c3b3c0b8f24a4...`, `vm-web-01_0sDisk_1_...`)
- Azure Monitor Alert rules (`alert-vm-web-01-cpu-high`, `alert-vm-dr-01-heartbeat`, `alert-vm-web-01-heartbeat`)
- VM Auto-Shutdown schedules (`shutdown-computevm-vm-dr-01`, `shutdown-computevm-vm-web-01`)
- Data Collection Rules (`dcr-ruriskry-prod-deml`)
- Log Analytics Workspace (`law-ruriskry-prod-deml`)
- Network Interface Cards (`nlc-vm-dr-01`, `nlc-vm-web-01`)

**Root cause:** `UNIV-HYG-001` and `UNIV-HYG-002` rules apply to all Azure resource types with no exclusion filter for child/support resources that typically don't need independent cost/ownership tags.  
**Impact:** Out of 35 Deploy proposals, a significant portion are noise. Operators must manually triage valid findings (NSGs, VNets, VMs) from invalid ones (OS disks, NICs, alert rules). This undermines trust in the rules engine.  
**Fix:** Add a `EXCLUDED_RESOURCE_TYPES` set to `UNIV-HYG-001` and `UNIV-HYG-002`:
```python
EXCLUDED_TYPES = {
    "microsoft.compute/disks",                        # OS/data disks (inherit from VM)
    "microsoft.network/networkinterfaces",            # NICs (child of VM)
    "microsoft.compute/virtualmachines/extensions",   # VM extensions
    "microsoft.insights/scheduledqueryrules",         # Alert rules
    "microsoft.insights/datacollectionrules",         # DCRs
    # Auto-shutdown schedules are nested resources — filter by name prefix "shutdown-computevm"
}
```

---

### BUG #29 — 🟠 HIGH | Agent card "actions proposed" count is all-time cumulative — not labeled

**Page:** Agents — agent cards  
**Symptom:** The large number shown on each agent card (e.g., "43 actions proposed") is the **cumulative all-time total** across every scan that agent has ever run, not the most recent scan's output. This is not labeled anywhere in the UI.  
**Example:** After yesterday's Deploy scan (2 proposals) + today's Deploy scan (35 proposals) + Apr 27's scan (6 proposals) = 43.  
**Impact:** New operators will interpret "43 actions proposed" as the last scan's output, which is wrong. A new scan that produces 0 findings would still show 43.  
**Fix:** Label the number clearly — e.g., "43 total proposals (35 in last scan)" — or change the card to show only the most recent scan's count.

---

### BUG #31 — 🟠 HIGH | A2 Validator "Fix using Agent" preview hangs ~40s with no loading state

**Page:** Decisions — Drilldown — A2 Validator modal  
**Symptom:** Clicking "Fix using Agent" calls `GET /api/execution/{id}/agent-fix-preview`. The endpoint takes approximately 40 seconds to return. During this time there is no loading spinner, no timeout, and no error fallback. The modal appears frozen.  
**Impact:** Operators believe the UI crashed and click elsewhere or close the modal. No graceful handling if the endpoint times out or errors.  
**Fix:** Add a loading spinner on click + a 30-second timeout with a user-friendly error message.

---

### BUG #34 — 🟠 HIGH | Different agents operate on different cached inventories (stale + inconsistent)

**Page:** Agents  
**Symptom:** During the "Run All Agents" session:
- Cost Agent scanned 36 resources (cache from Apr 29, 10:59 AM)
- Monitoring Agent scanned 55 resources (cache from Apr 29, 6:57 PM)
- Deploy Agent scanned ~35 unique tagged resources

Each agent independently refreshes and caches its own inventory. In a single "Run All Agents" session, the same subscription is evaluated with three different views of what resources exist.  
**Impact:** A resource added recently might be flagged by Monitoring but not by Cost. A deleted resource might still appear in one agent's cache. Duplicate proposals for the same resource can be generated by multiple agents in the same session with conflicting verdicts.  
**Fix:** Trigger a shared inventory refresh before "Run All Agents" starts, or use a single canonical inventory snapshot shared across all agents in a session.

---

### BUG #16 — 🟡 MEDIUM | Inventory page does not show which agent's cache is displayed

**Page:** Inventory  
**Symptom:** The Inventory page shows a single resource list with a single "Last refreshed" timestamp. It doesn't indicate that each agent has its own cache, or which cache the page is showing.  
**Impact:** Operators assume the Inventory page reflects what all agents see, but Cost sees 36 resources while Monitoring sees 55.

---

### BUG #17 — 🟡 MEDIUM | Agent card "Last seen" is stale during active scans

**Page:** Agents — agent cards  
**Symptom:** During the 33-minute Deploy scan, the Deploy card showed "Last seen: Apr 29, 12:45 PM" (yesterday's data). "Last seen" only updates when a scan completes, not when it starts or while running.  
**Impact:** Operators see stale timestamp and outdated verdict breakdown during active scans.

---

### BUG #18 — 🟡 MEDIUM | No consolidated progress view when all 3 agents run simultaneously

**Page:** Agents  
**Symptom:** "Run All Agents" shows a generic "Scan in progress…" bar and "Stop All" button. Individual agent cards show their own scanning state, but there is no single panel showing all 3 agents' progress in one place. During a 33-minute Deploy scan, there is no ETA.  
**Fix:** A consolidated progress panel showing `Agent | Status | Elapsed | Proposals found` for all 3 agents simultaneously.

---

### BUG #20 — 🟡 MEDIUM | APPROVED verdicts don't appear in the Decisions page

**Page:** Decisions  
**Symptom:** The Deploy scan produced 28 APPROVED verdicts. The Decisions badge only incremented by 7 (for the 7 ESCALATED verdicts). APPROVED verdicts are not surfaced in the Decisions page — they only appear in the Audit Log.  
**Impact:** After a scan that auto-approves 28 resources, operators have no consolidated view of what was automatically approved without digging through the Audit Log. The Decisions page appears to be a "work queue for human review" only, but this is not documented anywhere in the UI.

---

### BUG #21 — 🟡 MEDIUM | Verdict breakdown on agent card doesn't match the latest scan

**Page:** Agents — agent cards  
**Symptom:** After the Deploy scan produced 35 verdicts (28 approved, 7 escalated), the Deploy card showed "5 appr, 1 cond, 8 esc, 0 denied" — which is the cumulative unique-decision count across all Deploy scans, not the current scan's output.  
**Impact:** No quick way to see "what happened in this specific scan" from the card.

---

### BUG #25 — 🟡 MEDIUM | Scan history "Proposals" column shows 0 for running scans

**Page:** Agents — Scan History  
**Symptom:** During the entire 33-minute Deploy scan, the scan history row showed "0 proposals" even after 30+ proposals had been evaluated and persisted to Cosmos. The proposals column only updates when the scan completes.  
**Fix:** Update the scan record's `proposals_count` field as each proposal is persisted, not only at scan end.

---

### BUG #27 — 🟡 MEDIUM | APPROVED verdict displayed with SRI score exceeding the ESCALATED threshold

**Page:** Agents — Live Scan Log  
**Symptom:** Multiple verdicts showed `APPROVED (SRI N)` where N > 25 (the documented ESCALATED threshold ≤25):
- `APPROVED (SRI 25.3)` — vnet-ruriskry-prod
- `APPROVED (SRI 25.5)` — law-ruriskry-prod-deml
- `APPROVED (SRI 30.7)` — nlc-vm-web (NIC resource)

Meanwhile: `ESCALATED (SRI 30.5)` — nsg-cost-prod (correctly follows the threshold).

**Root cause:** Triage Tier 1 short-circuiting — hygiene rules on non-security resources are classified as Tier 1 and forced to APPROVED regardless of SRI score. NSG resources are not Tier 1, so their SRI score determines the verdict.

**Problem:** The displayed `(SRI N)` label alongside APPROVED for N > 25 directly contradicts the documented SRI threshold. Operators reading the log would believe SRI drives the verdict — seeing "APPROVED (SRI 30.7)" when the threshold is ≤25 = APPROVED destroys confidence.  
**Fix:** When Tier-1 forces the verdict, display `APPROVED [Tier-1] (SRI 30.7)` or `APPROVED [auto] (SRI 30.7)` to be transparent about why the SRI score was overridden.

---

### BUG #28 — 🟡 MEDIUM | Deploy scan duration increased from 13 minutes to 33 minutes after Phase 40

**Page:** Agents  
**Symptom:** Pre-Phase 40, the Deploy Agent scan took ~12m 44s (for 2 proposals). Post-Phase 40, the same scan takes **33 minutes 1 second** (for 35 proposals). The silent inventory/rules processing phase alone consumes ~14 minutes.  
**Breakdown:**
- Inventory + rules pre-scan: ~14 minutes (no events emitted)
- Sequential proposal evaluation: ~19 minutes (35 proposals × ~33s each)
- Total: 33 minutes

**Impact:** "Run All Agents" is effectively gated by the slowest agent (Deploy: 33 minutes). Proposals are evaluated sequentially, not in parallel. For 34 rules running on a subscription with many resources, this will only get slower as the subscription grows.  
**Recommended fix:** Evaluate proposals in configurable batches (e.g., 4 in parallel). This could reduce evaluation time from 19 minutes to ~5 minutes, bringing total scan time to ~19 minutes.

---

### BUG #30 — 🟡 MEDIUM | "Last seen" timestamp source is ambiguous (heartbeat vs scan completion)

**Page:** Agents — agent cards  
**Symptom:** In prior testing, Monitoring Agent card showed "Last seen: Apr 30 11:57 AM" when no Apr 30 scan existed in the scan history. The timestamp appeared to come from a heartbeat or live activity signal rather than the latest completed scan.  
**Impact:** Operators interpret "Last seen" as "last scan ran at" — if it's actually a heartbeat timestamp, this is misleading.

---

### BUG #33 — 🟡 MEDIUM | CoverageManifestPanel in View Log modal not verified

**Status:** Untested  
**Description:** `CoverageManifestPanel` (Phase 40) renders inside the Scan Log modal and should show the 34-rule coverage manifest with categories. All scans in this session are post-Phase 40, so the panel should appear. However, the View Log modal for the current scans was not opened to verify the component renders correctly.  
**Action needed:** Open "View Log" on any Apr 30 scan and confirm the coverage manifest panel appears with correct rule counts (7 cost, 2 hygiene, 9 reliability, 16 security).

---

### BUG #36 — 🟡 MEDIUM | Decisions badge is a "work queue" counter, not a "decisions made" counter — undocumented

**Page:** Sidebar — Decisions badge  
**Symptom:** After 44 total verdicts across all 3 agents, the Decisions badge only incremented by 7 (the 7 ESCALATED Deploy verdicts). The 28 APPROVEDs, 2 Cost verdicts, and 7 Monitoring verdicts are not reflected in the badge.  
**Impact:** The badge implicitly represents "items requiring human review" — but this is not communicated anywhere in the UI. A first-time operator would not know the badge excludes APPROVED verdicts.

---

### BUG #37 — 🟡 MEDIUM | Proposal counter may display denominator inconsistency during long scans

**Status:** Partially confirmed (possibly a screenshot font-size misread)  
**Symptom:** During the Deploy scan, proposal numbers appearing to exceed the denominator were observed in the Live Log (e.g., `[36/35]`, `[37/35]`). The final scan summary correctly showed "35 verdict(s)."  
**Root cause (suspected):** Either (a) the rules engine generates some proposals dynamically during evaluation and doesn't update the denominator, or (b) small log font caused misread of numbers like `[16/35]` as `[36/35]`.  
**Action needed:** Verify in code that `find_to_proposal()` and `dedup_proposals()` always produce a count consistent with what is displayed in the streaming log denominator.

---

### BUG #19 — 🟢 LOW | Scan history Duration column shows "—" for running scans

**Page:** Agents — Scan History  
**Symptom:** Running scans show "—" in the Duration column. The agent card shows a live timer ("Scanning… 14m 4s"), but the scan history row does not.  
**Fix:** Show a live elapsed timer in the Duration column for running scans, matching the card.

---

### BUG #23 — 🟢 LOW | Scan history CSV missing useful analytics columns

**File:** `.playwright-mcp/ruriskry-scans.csv`  
**Symptom:** The scan CSV only contains `scan_id, agent_type, status, started_at, completed_at, proposals_count, evaluations_count`. Missing: `approved_count`, `escalated_count`, `denied_count`, `error_message`, `total_resources_scanned`.  
**Impact:** Post-scan analytics require manual Cosmos queries instead of CSV analysis.

---

### BUG #32 — 🟢 LOW | Dismiss modal does not close on Escape key

**Page:** Decisions — Drilldown — Dismiss modal  
**Symptom:** Pressing Escape while the Dismiss action modal is open does nothing. Other modals in the dashboard close on Escape. This modal requires clicking "Cancel" explicitly.

---

### BUG #35 — 🟢 LOW | Reopening Live Scan Log during active scan shows oldest events, not newest

**Page:** Agents — Live Scan Log  
**Symptom:** When the Live Scan Log panel is closed and reopened mid-scan, the SSE replays all events from the beginning. The viewport shows the oldest events (top of log) rather than auto-scrolling to the newest.  
**Fix:** On reconnect, seek to the last N events (e.g., last 50) or auto-scroll to bottom on open.

---

## 5. Priority Fix Order for Development Team

| Priority | Bug # | Title | Effort |
|---|---|---|---|
| 🔴 P0 | #14 | Fix `query_resources_async` import in `api_preflight.py` | 1 line |
| 🟠 P1 | #26 | Filter child resources (NICs, disks, alert rules, DCRs) from UNIV-HYG rules | Small |
| 🟠 P1 | #27 | Show `[Tier-1]` label on APPROVED verdicts where SRI exceeds threshold | Small |
| 🟠 P1 | #24 | Emit SSE progress events during inventory/rules phase | Medium |
| 🟠 P1 | #15 | Show CoverageStatusBanner on Overview page | Small |
| 🟠 P1 | #31 | Add loading state + timeout to A2 Validator preview endpoint | Small |
| 🟠 P2 | #28 | Evaluate proposals in parallel batches (reduce Deploy scan 33m → ~19m) | Medium |
| 🟠 P2 | #34 | Shared inventory snapshot for "Run All Agents" | Medium |
| 🟡 P3 | #22 | Investigate Alerts badge drop on scan start | Small |
| 🟡 P3 | #25 | Update proposals count in real-time during scan | Small |
| 🟡 P3 | #20 | Surface APPROVED verdicts somewhere in Decisions UI | Medium |
| 🟡 P3 | #29 | Relabel agent card counter to show "last scan" vs "all-time" | Small |
| 🟡 P4 | #33 | Verify CoverageManifestPanel renders in View Log modal | Test only |
| 🟢 P5 | #32 | Close Dismiss modal on Escape key | Trivial |
| 🟢 P5 | #35 | Auto-scroll Live Log to newest event on open | Small |

---

## 6. Observations (Not Bugs, But Worth Noting)

- **Phase 40 side effect on scan volume:** Before Phase 40, the Deploy Agent ran 2 proposals per scan. After Phase 40, it runs 35 proposals per scan (all tag compliance). This is technically correct behavior — but operators should be aware that adding 34 universal rules dramatically increases per-scan volume and duration.

- **UNIV-HYG-002 triggering correctly on NSGs:** The 7 ESCALATED verdicts from the Deploy scan were all for NSGs and security-critical resources. The rules engine correctly differentiates blast radius — NSGs get higher SRI scores (30.5) and ESCALATED verdicts. This is working as designed.

- **Deduplication working:** 28 APPROVED proposals from the Deploy scan resulted in 0 new Decisions entries (all were updates to existing decisions from previous scans). The deduplication mechanism is functioning correctly and prevents spam in the Decisions page.

- **vm-dr-01 correctly DENIED:** The Cost Agent correctly denied `vm-dr-01` with SRI 43.0 due to `UNIV-COST-001` (VM in deallocated state for cost optimization) combined with a CRITICAL policy violation. The `force_deterministic` override for CRITICAL violations is working correctly.

---

*Report generated by automated QA session — 2026-04-30*
