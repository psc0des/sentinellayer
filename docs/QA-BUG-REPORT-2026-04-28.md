# RuriSkry Governance Dashboard — QA Bug Report

**Tester:** Senior QA (Independent Review)
**Date:** 2026-04-28
**Environment:** Production
**Frontend:** `https://gentle-water-02128160f.7.azurestaticapps.net`
**Backend:** `https://ruriskry-core-backend-psc0de.icymoss-9765d73f.eastus2.azurecontainerapps.io`
**Method:** Full UI pass (human-simulated via Playwright automation) — Overview → Inventory → Agents → Decisions → Audit Log → Alerts → Admin. Every KPI cross-verified against live API responses. No hardcoded values trusted.
**Screenshots:** `qa-01` through `qa-16` in project root.

---

## Severity Legend

| Level | Meaning |
|-------|---------|
| **CRITICAL** | Data integrity failure, security hole, or operator will make a wrong decision based on false information |
| **HIGH** | Significant functional gap that blocks correct use of the product |
| **MEDIUM** | Wrong display, UX breakdown, or missing feature that degrades usability |
| **LOW** | Minor inconsistency or cosmetic issue |

---

## Bug Count Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 4 |
| HIGH | 7 |
| MEDIUM | 6 |
| **Total** | **17** |

---

## CRITICAL BUGS

---

### BUG-002 — Dashboard "Pending Reviews" Always Shows Zero

**Page:** Overview Dashboard
**Severity:** CRITICAL
**Screenshot:** `qa-01-initial-load.png`

**What the UI shows:** KPI card reads **"0 Pending reviews / All clear"** in green.

**What the API returns:** `GET /api/execution/pending-reviews` → **4 escalated decisions** actively awaiting human review.

**Impact:** Operators believe the HITL queue is empty and walk away. Escalated decisions sit unactioned indefinitely. The entire Human-In-The-Loop safety loop depends on operators noticing items in the queue. This is a governance safety failure.

**Steps to reproduce:**
1. Load the Overview page
2. Observe "Pending reviews" KPI card shows 0 / All clear (green)
3. `curl https://<backend>/api/execution/pending-reviews` → returns 4 items

**Expected:** Card shows "4 Pending reviews" and triggers attention styling (non-green).

---

### BUG-007 — ERROR Scan Modal Displays "Scan Completed — No Issues Found"

**Page:** Agents → Scan History → Log Modal
**Severity:** CRITICAL
**Screenshot:** `qa-06-agents-log-modal.png`

**What the UI shows:** A scan that ran for 100 minutes 2 seconds (timeout signature) with `status: error` shows a green banner reading **"Scan completed — no issues found"**. The modal footer then contradicts itself: "Status: error | 0 evaluations."

**Impact:** Operators reading a scan log to investigate a failure are told everything is fine. They will dismiss a real infrastructure scan failure as a data fluke. Silent governance failures are the worst kind.

**Steps to reproduce:**
1. Go to Agents page
2. Find any scan showing ERROR status in scan history
3. Click to open the log modal
4. Observe: green "completed — no issues" banner + contradictory "Status: error" footer

**Expected:** Error scans should show an error banner. "No issues found" must only appear for completed scans with zero findings.

---

### BUG-012 — Risk Highlights Contains Factually Wrong Text

**Page:** Decisions → Drilldown panel → Risk Highlights section
**Severity:** CRITICAL
**Screenshot:** `qa-09-decision-drilldown.png`

**What the UI shows:** Risk Highlights reads **"elevated policy risk without explicit violations"**.

**What the API confirms:** The same decision has two explicit, named policy violations: `POL-CRIT-001 HIGH` and `POL-PROD-003 LOW`.

**Impact:** This is a governance dashboard. Operators use the Risk Highlights text to brief stakeholders and inform escalation decisions. Telling them "no explicit violations" when two violations are formally recorded is factually false and can result in an incorrect risk assessment being communicated up the management chain.

**Steps to reproduce:**
1. Open any decision that has policy violations listed in the violations panel
2. Read the Risk Highlights text
3. Observe it states "without explicit violations" despite violations being present

**Expected:** Risk Highlights should accurately reflect whether violations were found. The text generation must read from the same violations array rendered in the UI.

---

### BUG-017 — `/api/config` Is Unauthenticated and Exposes Azure Subscription ID

**Page:** N/A (API endpoint)
**Severity:** CRITICAL

**Verified:** `curl https://<backend>/api/config` with **no authentication headers** → HTTP 200 with full system config:
```json
{
  "subscription_id": "<azure-subscription-id>",
  "mode": "production",
  "llm_timeout": 600,
  "max_concurrent_scans": 3,
  ...
}
```

**Impact:** Any internet user who can reach the backend URL (which is public) can enumerate the Azure subscription ID, LLM timeout, concurrency limits, and operational mode without credentials. The subscription ID alone is enough to narrow targeted Azure attacks and enumerate public resources under that subscription.

**Fix:** Add the existing auth middleware decorator to the `/api/config` route. One line.

---

## HIGH BUGS

---

### BUG-003 — Cosmos DB Internal Metadata Leaked in `/api/agents` Response

**Page:** N/A (API endpoint, consumed by Agents page)
**Severity:** HIGH (Security)

**What the API returns:** Every agent document in `GET /api/agents` includes raw Cosmos DB internal fields:
- `_rid` — internal resource ID (Cosmos topology)
- `_self` — direct Cosmos resource link (can be used to construct direct DB calls)
- `_etag` — document version hash
- `_attachments` — attachment link
- `_ts` — internal timestamp

**Impact:** Exposes internal database addressing structure to any authenticated frontend client. `_self` is a live resource link. This is an unnecessary information disclosure that violates the principle of least privilege and could aid an attacker who has compromised a user account.

**Fix:** Strip Cosmos internal fields in the API serialization layer before returning. Use a response model that only includes application-level fields.

---

### BUG-008 — Agent Card Verdict Breakdown Silently Drops APPROVED_IF and PENDING

**Page:** Agents → Agent cards
**Severity:** HIGH
**Screenshot:** `qa-05-agents.png`

**What the UI shows:**
- Monitoring agent: "8 proposed" — breakdown: Approved(4) + Escalated(2) + Denied(0) = **6 (2 unaccounted)**
- Deploy agent: "6 proposed" — breakdown: Approved(4) + Escalated(1) + Denied(0) = **5 (1 unaccounted)**

**Root cause:** `APPROVED_IF` and `PENDING` verdict types are not included in the card breakdown counts. They are silently omitted.

**Impact:** Operators cannot trust the verdict summary on agent cards. The numbers visibly don't add up, which undermines confidence in the entire dashboard.

**Fix:** Add `APPROVED_IF` and `PENDING` to the breakdown counts (and add corresponding labels/colors to the breakdown UI).

---

### BUG-010 — Playbook Endpoint Returns 404 for Escalated Decisions

**Page:** Decisions → Drilldown panel → Remediation Playbook section
**Severity:** HIGH
**Screenshot:** `qa-09-decision-drilldown.png`

**What the UI shows:** "Playbook not available" with a console 404 error for `GET /api/decisions/{id}/playbook`.

**Impact:** The Tier 3 Playbook Generator (Phase 34D) exists and passes unit tests, but the endpoint integration is broken for at least escalated decisions. Operators clicking into the highest-stakes decisions (ESCALATED) to understand the recommended remediation get nothing — exactly the moment they need it most.

**Steps to reproduce:**
1. Find any ESCALATED decision
2. Open drilldown panel
3. Scroll to "Remediation Playbook" section — shows "not available"
4. Check browser console — HTTP 404

---

### BUG-013 — APPROVED_IF Renders with ✗ (Denied) Icon in Audit Log

**Page:** Audit Log → Outcome column
**Severity:** HIGH
**Screenshot:** `qa-12-audit-log.png`

**What the UI shows:** Decisions with `APPROVED_IF` verdict display the **✗ icon** — the same icon used for DENIED decisions.

**Impact:** An operator scanning the Audit Log reads a conditionally approved action as rejected. They may attempt to re-run scans, escalate to management, or block an operation that was actually approved with conditions. In a governance context, misreading approval as denial has real operational consequences.

**Fix:** Map `APPROVED_IF` to its own icon (e.g., ⚠ or ✓ with a condition indicator). It is not a denial.

---

### BUG-014 — Audit Log Outcome Summary Labels APPROVED_IF as "Denied"

**Page:** Audit Log → Drilldown panel → Outcome Summary
**Severity:** HIGH
**Screenshot:** `qa-13-audit-log-drilldown.png`

**What the UI shows:** Outcome summary panel counts an `APPROVED_IF` decision as **"Denied: 1"**.

**Verified:** Deploy agent had zero actual denials. The 1 in the "Denied" bucket was the nsg-east-prod `APPROVED_IF` decision.

**Impact:** Compounds BUG-013. Now the summary panel explicitly uses the word "Denied" for a conditionally approved decision. A manager reviewing the audit summary would take a completely different escalation action based on this label.

**Fix:** Same root cause as BUG-013 — the verdict→label mapping is missing the `APPROVED_IF` case and is falling through to a denied/unknown bucket.

---

### BUG-015 — Active Alert Count Contradicts Between Overview and Alerts Page

**Page:** Overview Dashboard vs. Alerts page
**Severity:** HIGH
**Screenshots:** `qa-01-initial-load.png`, `qa-14-alerts.png`

**What the Overview shows:** Alerts KPI card — **"Active: 0"**

**What the Alerts page shows:** Header — **"17 active"** (same 17 unresolved alerts, some over 2 days old, 0% investigation rate)

**Impact:** An operator checking the Overview concludes no alerts need attention and never navigates to the Alerts page. They miss 17 unresolved, uninvestigated alerts. The Overview is supposed to be the "all clear" dashboard — if it says 0 active alerts, operators trust it.

---

### BUG-016 — Admin "Danger Zone" Accessible to All Authenticated Users (No RBAC)

**Page:** Admin page (`/admin`)
**Severity:** HIGH (Security)
**Screenshot:** `qa-16-admin.png`

**What the UI shows:** Admin page with "Reset All Data" Danger Zone button appears in the navigation for **every authenticated user**. No role check, no admin-only route guard.

**Note:** The reset API endpoint correctly returns 401 for unauthenticated callers — but a regular user's bearer token is valid auth. The gate only blocks unauthenticated callers, not unauthorized ones (non-admin authenticated users).

**Impact:** Any user with a login can navigate to Admin and trigger a full system data reset. One wrong click by any user wipes all governance history.

**Fix:** Add route-level RBAC guard (e.g., check for `admin` role claim in the Azure Static Web Apps auth token). Hide the Admin nav item for non-admin users.

---

## MEDIUM BUGS

---

### BUG-004 — Inventory Stale Warning Uses Different Threshold Than Backend

**Page:** Inventory page
**Severity:** MEDIUM
**Screenshot:** `qa-02-inventory.png`

**What the UI shows:** Warning banner: "Inventory is old — refresh recommended"

**What the API says:**
- `GET /api/inventory/status` → `stale: false` (inventory age: ~23.4 hours)
- `GET /api/config` → `inventory_stale_hours: 24`

**Impact:** Frontend triggers a staleness warning ~30 minutes before the backend considers data stale. Operators get a warning for data the system considers fresh, creating alert fatigue. When warnings fire incorrectly, operators learn to ignore them — including real ones.

**Fix:** Frontend should call `/api/inventory/status` and render the warning only if `stale: true`. Do not hardcode a separate threshold in the frontend.

---

### BUG-005 — Resource Group Count Briefly Shows Wrong Number on Load

**Page:** Inventory page
**Severity:** MEDIUM
**Screenshot:** `qa-02-inventory.png`

**What the UI shows:** On initial load, the Inventory header briefly shows **(6)** resource groups before settling to the correct **(5)**.

**Root cause:** Count is computed from a partially-loaded or non-deduped data state during render.

**Fix:** Derive the displayed count from the final, settled data array — not from an intermediate state. Use a loading skeleton until data is confirmed loaded.

---

### BUG-006 — Scan Timeouts Are Silent — No Alert, No Label, No Notification

**Page:** Agents → Scan History
**Severity:** MEDIUM
**Screenshot:** `qa-06-agents-log-modal.png`

**Observation:** At least 3 scans ran for exactly ~100 minutes before failing — a clear LLM/scan timeout signature. No alert was fired in the Alerts page, no notification appeared, no "TIMEOUT" label in the scan history row (just "ERROR").

**Impact:** Operators have no way to know that governance evaluations timed out unless they manually click into each scan log and read the duration. The alerting infrastructure exists but is not wired to internal scan failures. Repeated silent timeouts indicate a systematic LLM capacity or configuration issue that would go undetected.

**Fix:** Fire an internal alert when a scan ends in ERROR/TIMEOUT. Label timeout scans distinctly from other error types in the scan history row.

---

### BUG-009 — Scan Log Modal Does Not Close on Escape Key

**Page:** Agents → Scan History → Log Modal
**Severity:** MEDIUM

**Steps to reproduce:**
1. Open any scan log modal
2. Press `Escape`
3. Modal remains open — all underlying page interactions are blocked until X is clicked

**Expected:** Standard browser/UX behaviour — `Escape` dismisses modal dialogs.

**Fix:** Add `onKeyDown` handler (or a `useEffect` with `window.addEventListener('keydown', ...)`) that calls the modal close function when `key === 'Escape'`.

---

### BUG-011 — Counterfactual Score Has Rounding Inconsistency

**Page:** Decisions → Drilldown panel
**Severity:** MEDIUM
**Screenshot:** `qa-09-decision-drilldown.png`

**What the UI shows:**
- Score badge: **27.3**
- Description text: **"would drop to 27.2"**

Two representations of the same computed value disagree by 0.1 due to inconsistent rounding.

**Fix:** Compute the display value once and pass it to both the badge and the description text. Do not format the number independently in two places.

---

### BUG-018 — "APPROVED_IF" Missing from Verdict Filter Dropdown

**Page:** Decisions page → Verdict filter dropdown
**Severity:** MEDIUM
**Screenshot:** `qa-08-decisions-all.png`

**What the UI shows:** Filter dropdown options: All verdicts / Approved / Escalated / Denied

**Reality:** 3 of 18 decisions (16.7%) have `APPROVED_IF` verdict. There is no way to filter to show only conditional approvals.

**Impact:** Operators cannot isolate APPROVED_IF decisions to verify whether the stated conditions have been met before operations proceed. This is exactly the workflow conditional approvals are designed to support.

**Fix:** Add "Approved If" as a filter option and map it to the `APPROVED_IF` verdict value.

---

## CROSS-CUTTING FINDING — APPROVED_IF Is Systemically Broken

**BUG-008, BUG-013, BUG-014, and BUG-018 share a single root cause:** the `APPROVED_IF` verdict type was added to the backend data model but the frontend verdict→label/icon/count mapping was never updated to handle it.

Fixing the mapping in one shared constant/utility will close all four bugs simultaneously. Recommend finding wherever the frontend maps verdict strings to display values (likely a `VERDICT_MAP` or similar object) and adding the `APPROVED_IF` case.

---

## Evidence

| Screenshot | What it shows |
|-----------|---------------|
| `qa-01-initial-load.png` | Overview with "0 Pending reviews" (BUG-002, BUG-015) |
| `qa-02-inventory.png` | Stale banner + count flash (BUG-004, BUG-005) |
| `qa-03-inventory-vm-expand.png` | VM row expansion |
| `qa-04-inventory-search.png` | Search filter working correctly |
| `qa-05-agents.png` | Agent card verdict math mismatch (BUG-008) |
| `qa-06-agents-log-modal.png` | ERROR scan with "no issues" banner (BUG-007, BUG-009) |
| `qa-07-decisions-from-agent.png` | Decisions filtered from agent link |
| `qa-08-decisions-all.png` | Full decisions list + missing APPROVED_IF filter (BUG-018) |
| `qa-09-decision-drilldown.png` | Drilldown: wrong risk text, rounding, playbook 404 (BUG-010, BUG-011, BUG-012) |
| `qa-10-verdict-json.png` | Raw verdict JSON from API |
| `qa-11-execution-status.png` | Execution/HITL status panel |
| `qa-12-audit-log.png` | APPROVED_IF shown with ✗ icon (BUG-013) |
| `qa-13-audit-log-drilldown.png` | "Denied: 1" for APPROVED_IF (BUG-014) |
| `qa-14-alerts.png` | 17 active alerts (contradicts Overview "Active: 0") (BUG-015) |
| `qa-15-alert-expand.png` | Alert detail expansion |
| `qa-16-admin.png` | Admin page visible to all users (BUG-016) |

---

## Priority Fix Order (Recommended)

1. **BUG-017** — Auth on `/api/config` (one-line fix, security critical, ship immediately)
2. **BUG-002** — Pending reviews count (HITL safety loop is broken)
3. **BUG-007** — Error scan shows success (operators are actively misled)
4. **BUG-012** — Risk Highlights false text (factually wrong governance data)
5. **BUG-013 + BUG-014 + BUG-008 + BUG-018** — APPROVED_IF mapping (one fix, four bugs close)
6. **BUG-016** — Admin RBAC (security, before wider rollout)
7. **BUG-010** — Playbook 404 (Phase 34D feature broken in integration)
8. **BUG-015** — Alert count contradiction (Overview trustworthiness)
9. **BUG-003** — Cosmos metadata leak (strip in serialization layer)
10. Remaining medium bugs (BUG-004 through BUG-011, BUG-018)

---

*Report generated from live production environment. All API responses captured during testing session on 2026-04-28.*
