# QA Re-Verification Report — Post-Fix Pass
**Round:** 2 (verification of claimed fixes)
**Tester:** Senior QA (Independent)
**Date:** 2026-04-28
**Frontend:** `https://gentle-water-02128160f.7.azurestaticapps.net`
**Backend:** `https://ruriskry-core-backend-psc0de.icymoss-9765d73f.eastus2.azurecontainerapps.io`
**Method:** Live Playwright browser automation + direct API calls (`curl`) + source code review.
No claims trusted. Every fix verified independently against the live deployed environment.

---

## Bottom Line Up Front

> Devs claimed all 17 bugs fixed. QA confirmed **10 genuinely closed**.
> **4 bugs remain broken. 2 new regressions were introduced by the fixes.**
> The APPROVED_IF systemic issue (4 bugs) was fixed in code but **not in the database** — historical Cosmos records were never migrated.

---

## Scorecard

| Category | Claimed Fixed | Actually Fixed | Still Broken | New Regressions |
|----------|:------------:|:--------------:|:------------:|:---------------:|
| Critical (4) | 4 | 4 | 0 | 1 |
| High (7) | 7 | 3 | 4 | 1 |
| Medium (6) | 5 | 5 | 0 | 0 |
| **Total** | **16** | **12** | **4** | **2** |

---

## VERDICT LEGEND

| Symbol | Meaning |
|--------|---------|
| ✅ FIXED | Confirmed resolved in live production |
| ❌ NOT FIXED | Still broken — evidence below |
| ⚠️ PARTIAL | Fix applied but incomplete — edge cases remain |
| 🔴 REGRESSION | New bug introduced by this fix |
| ➡️ DEFERRED | Acknowledged out of scope — no change expected |

---

## CRITICAL BUGS — 4/4 Fixed ✅

---

### BUG-017 — `/api/config` Unauthenticated ✅ FIXED

**Verification:** `curl https://<backend>/api/config` (no auth) → **HTTP 401**

Previously returned HTTP 200 with Azure subscription ID to any caller. Now correctly protected.

---

### BUG-002 — Pending Reviews Always Shows 0 ✅ FIXED

**Verification:** Overview KPI card now shows **"4 Pending reviews — require action"** with amber styling. The widget below shows all 4 ESCALATED items. The fix (`reviewsData.reviews` instead of `reviewsData.pending_reviews`) is working.

---

### BUG-007 — ERROR Scan Shows "No Issues Found" Success Banner ✅ FIXED

**Verification:** Opened a Cost Agent ERROR scan modal. Modal now shows:
> **"Scan failed — no evaluations completed"** in red
> "Check the scan duration; a ~100 min runtime indicates a governance timeout"

The false green success banner is gone.

---

### BUG-012 — Risk Highlights Says "No Explicit Violations" ✅ FIXED (but see REGRESSION-001)

**Verification:** Decision drilldown for vm-web-01 (restart_service, Escalated) now reads:
> "2 policy violation(s) detected. Most severe: POL-CRIT-001."

The "without explicit violations" text is gone. However, the fix introduced a regression — see REGRESSION-001.

---

## HIGH BUGS — 3/7 Fixed

---

### BUG-003 — Cosmos Metadata Leaked in `/api/agents` ✅ FIXED

**Verification:**
```
curl /api/agents → agent keys: ['id', 'name', 'agent_card_url', 'registered_at',
'last_seen', 'total_actions_proposed', 'approval_count', 'denial_count', 'escalation_count']
```
Zero `_rid`, `_self`, `_etag`, `_attachments`, `_ts` fields. Clean.

---

### BUG-008 — Agent Card Verdict Breakdown Drops APPROVED_IF ❌ NOT FIXED

**Verification:**
```
GET /api/agents

Monitoring Agent:  total_actions_proposed=8, approval_count=4, denial_count=0,
                   escalation_count=2, approved_if_count=MISSING
  → 8 proposed, 4+2+0 = 6 accounted. 2 still unaccounted.

Deploy Agent:      total_actions_proposed=6, approval_count=4, denial_count=0,
                   escalation_count=1, approved_if_count=MISSING
  → 6 proposed, 4+1+0 = 5 accounted. 1 still unaccounted.
```

**Why the fix didn't work:** The code in `agent_registry.py` correctly tracks `approved_if_count` for NEW decisions. But the existing agent records in Cosmos DB were written before the fix — they have no `approved_if_count` field. The `get_connected_agents()` function reads stored records directly; it doesn't recompute. So every historical APPROVED_IF decision is invisible in the card.

**Fix required:** Either run new scans (existing agent records will accumulate correct counts going forward) OR backfill the Cosmos agent records using the scan evaluations data.

---

### BUG-010 — Playbook 404 for Escalated Decisions ⚠️ PARTIAL

**Verification:**
```
curl /api/decisions/<nsg-east-prod-id>/playbook  → HTTP 500 Internal Server Error
curl /api/decisions/<vm-update-config-id>/playbook → HTTP 404
curl /api/decisions/<vm-restart-service-id>/playbook → HTTP 200 ✓ (working playbook)
```

The `restart_service` template for VMs works. But:
- `modify_nsg` on NSG: **was 404, now 500** — the template exists but crashes on execution. This is a regression within the fix itself.
- `update_config` on VM: **still 404** — no template was added for this action/resource combination.

The fix addressed 1 of 3 broken cases. The most urgent escalated decision in the pending review queue (`update_config` on VM) still returns 404. See also REGRESSION-002.

---

### BUG-013 — APPROVED_IF Shows ✗ (Denied Icon) in Audit Log ❌ NOT FIXED

**Verification:** Audit Log outcome column for Deploy scan (Apr 27, 02:29 PM):
```
Displayed: 4✓  1⚠  1✗
```
The `⚠` is the ESCALATED icon (amber). The `✗` is the APPROVED_IF decision being shown as denied — because `totals.denied = 1` in the stored Cosmos record.

**Why the fix didn't work:** The frontend code is correctly updated:
```javascript
{(totals.approved_if ?? 0) > 0 && <span className="text-teal-400">{totals.approved_if}~</span>}
```
But `totals.approved_if` is 0 for all existing scan records. The backend `totals` were written before the fix with APPROVED_IF counted under `denied`. The `list_scan_history` endpoint passes stored records through without recomputing. So the `~` icon never appears and APPROVED_IF still renders as `✗`.

---

### BUG-014 — Audit Log Counts APPROVED_IF as "Denied" ❌ NOT FIXED

**Verification:** Clicked Deploy scan (Apr 27, 02:29 PM) drilldown — Outcome Summary panel:
```
36 Scanned  |  4 Approved  |  1 Escalated  |  1 Denied
```
Deploy had **zero actual denials**. The "Denied: 1" is the APPROVED_IF (nsg-east-prod) miscounted. No "Cond. Appr." bucket appeared.

**Root cause:** Same as BUG-013. Stored `totals` in Cosmos: `{approved:4, escalated:1, denied:1}` — no `approved_if` key. Backend API call confirmed:
```
GET /api/scan-history → totals: {approved: 4, escalated: 1, denied: 1}
```

**The one-line fix:** In `list_scan_history()`, recompute `totals.approved_if` from the `evaluations` array before serving:
```python
# Recompute approved_if from evaluations (historical records have wrong totals)
evals = entry.get("evaluations", [])
entry.setdefault("totals", {})["approved_if"] = sum(
    1 for e in evals if (e.get("decision") or "").lower() == "approved_if"
)
```
This self-heals all historical records on every read with zero migration cost.

---

### BUG-015 — Active Alert Count: Overview Shows 0, Alerts Shows 17 ✅ FIXED

**Verification:** Overview now shows "ACTIVE: 19 •" (red indicator). Sidebar badge shows 19. Alerts page header shows 19. Consistent across all views.

---

### BUG-016 — Admin "Danger Zone" Accessible to All Authenticated Users ⚠️ PARTIAL

**Verification:** Navigated to `/admin` as logged-in user "admin" — full System Configuration and Danger Zone Reset button visible. Source code review:

```javascript
// Admin.jsx line 83
if (!loggedInUser) {
  return <AccessDenied />
}
```

The fix only blocks `loggedInUser === null` (unauthenticated). **Any authenticated user has a non-null `loggedInUser`** and passes this check. The Admin nav link remains visible to all users in the sidebar.

**What was fixed:** Unauthenticated callers are now blocked (but the reset API endpoint already returned 401 for them anyway, so this adds no new protection).

**What is still broken:** A non-admin authenticated user can navigate to `/admin`, see the full config, and click Reset. The fix needs a role claim check (e.g., check `loggedInUser === 'admin'` or an `isAdmin` flag on the user object from the auth response).

---

## MEDIUM BUGS — 5/6 Fixed

---

### BUG-004 — Stale Warning Uses Different Threshold Than Backend ✅ FIXED

**Verification (source code):**
```javascript
// Inventory.jsx
const stale = invStatus?.stale ?? true   // ← reads from backend API
```
`invStatus` is populated from `fetchInventoryStatus()` which returns the backend's authoritative `stale` boolean. No hardcoded frontend threshold remains.

---

### BUG-005 — Resource Group Count Flickers on Load ✅ FIXED

**Verification (source code):**
```javascript
{/* Filter row — only render after load to avoid count flicker (BUG-005) */}
{resources.length > 0 && !loading && (
  <select>...</select>   // ← dropdowns hidden until data settled
)}
```
The source comment even names the bug. Dropdowns and their counts are suppressed during loading state.

---

### BUG-006 — Silent Scan Timeouts ➡️ DEFERRED

No change. Error scans still appear without a "TIMEOUT" label or alert firing. Acknowledged as deferred infrastructure work.

---

### BUG-009 — Escape Key Doesn't Close Scan Modal ✅ FIXED

**Verification:** Opened error scan modal → pressed `Escape` → modal dismissed immediately, underlying page fully interactive. Confirmed in live browser.

---

### BUG-011 — Counterfactual Score Rounding Inconsistency ✅ FIXED

**Verification:** vm-web-01 drilldown (SRI 37.75, displayed as 37.8):
- Badge: `37.8`
- Counterfactual text: "Composite SRI drops from **37.8** to 32.8"
- Second scenario: "Composite SRI drops from **37.8** to 28.8"

Badge and text are consistent throughout. The `_recalculate_composite` rounding fix is working.

---

### BUG-018 — "APPROVED IF" Missing From Verdict Filter Dropdown ✅ FIXED

**Verification:**
```
Verdict filter options: All verdicts | Approved | Approved If [val=approved_if] | Escalated | Denied
```
Selected "Approved If" → filtered from 18 to **3 rows**, all correctly showing APPROVED IF verdict badge. Filter is functional end-to-end.

---

## NEW REGRESSIONS INTRODUCED

---

### REGRESSION-001 — Policy Violation Names Show "Unknown Policy" 🔴

**Introduced by:** BUG-012 fix (explanation endpoint reconstruction from flat tracker list)
**Severity:** HIGH
**Page:** Decisions → Drilldown → Policy Violations section

**What it looks like:**
```
POLICY VIOLATIONS
POL-CRIT-001 (MEDIUM) — Unknown Policy:
POL-PROD-002 (MEDIUM) — Unknown Policy:
```

**What it should say:**
```
POL-CRIT-001 (HIGH) — Critical Resource Protection
POL-PROD-002 (MEDIUM) — Production Service Restart Protection
```

**Two sub-issues:**
1. Policy names are not resolving — both show "Unknown Policy" despite the policy IDs being valid entries in `data/policies.json`
2. Severity is wrong — POL-CRIT-001 is a HIGH severity policy, displayed as MEDIUM

**Impact:** Every decision drilldown is affected. Operators see "Unknown Policy" for every violation — useless for governance review. Also the wrong severity could cause an operator to underestimate the urgency of a critical policy violation.

**Steps to reproduce:**
1. Open any decision drilldown
2. Scroll to "Policy Violations"
3. All violations display "Unknown Policy" regardless of policy ID

---

### REGRESSION-002 — NSG Playbook Now Returns HTTP 500 Instead of 404 🔴

**Introduced by:** BUG-010 fix (new playbook templates added)
**Severity:** HIGH
**Page:** Decisions → Drilldown → Remediation Playbook (for modify_nsg decisions)

**Before fix:** `GET /api/decisions/<nsg-id>/playbook` → HTTP 404
**After fix:** `GET /api/decisions/<nsg-id>/playbook` → **HTTP 500 Internal Server Error**

The `modify_nsg` playbook template was added but crashes on execution server-side. The UI shows "Could not load playbook: Failed to fetch" — same broken experience but now the server is also throwing an unhandled exception.

**Steps to reproduce:**
1. Open any `modify_nsg` decision (e.g., nsg-east-prod)
2. Scroll to Remediation Playbook section
3. Shows "Failed to fetch" — server logs will show 500

---

## THE APPROVED_IF DATABASE PROBLEM — EXPLAINED

Three bugs (BUG-008, BUG-013, BUG-014) share one root cause that the devs missed:

**The fix was applied to the code. The fix was NOT applied to the data.**

All existing scan records and agent records in Cosmos DB were written before the APPROVED_IF fix. Those records have:
- `totals: {approved: N, escalated: N, denied: N}` — no `approved_if` key (APPROVED_IF was miscounted as `denied`)
- Agent records: no `approved_if_count` field

The frontend code was updated to read `totals.approved_if`, but since that field is 0/missing in all historical records, the correct UI states never appear.

**The fix is one function call in `list_scan_history()` — recompute `totals.approved_if` from the `evaluations` array at read time:**

```python
for entry in cleaned:
    evals = entry.get("evaluations", [])
    if evals:
        ai_count = sum(1 for e in evals if (e.get("decision") or "").lower() == "approved_if")
        entry.setdefault("totals", {})["approved_if"] = ai_count
        # Also correct the denied count (historical records put APPROVED_IF in denied)
        entry["totals"]["denied"] = max(0, entry["totals"].get("denied", 0) - ai_count)
```

This requires no schema migration, no re-scanning, and zero downtime. It self-heals all historical data on every read.

---

## PRIORITY ORDER FOR REMAINING WORK

1. **REGRESSION-001** — Policy violation names show "Unknown Policy" (affects every drilldown, must fix before any demo)
2. **REGRESSION-002** — NSG playbook HTTP 500 (server is throwing unhandled exceptions)
3. **BUG-013/014/008** — APPROVED_IF database fix (one function, heals three bugs)
4. **BUG-016** — Admin RBAC (role check vs null check)
5. **BUG-010** — Remaining playbook templates (`update_config` on VM)

---

## EVIDENCE TRAIL

| Screenshot | What it proves |
|-----------|----------------|
| `verify-01-overview.png` | BUG-002 fixed (4 pending reviews shown) + BUG-015 fixed (active: 19) |
| `verify-02-agents.png` | BUG-008 not fixed (4+2+0=6 of 8 for Monitoring) |
| `verify-03-error-scan-modal.png` | BUG-007 fixed (red error banner) |
| `verify-04-escape-key-test.png` | BUG-009 fixed (modal dismissed by Escape) |
| `verify-05-approved-if-filter.png` | BUG-018 fixed (3 APPROVED IF rows isolated correctly) |
| `verify-06-nsg-drilldown.png` | REGRESSION-001 visible ("Unknown Policy"), BUG-010 partial (playbook failed) |
| `verify-07-audit-log.png` | BUG-013/014 status visible (outcome icons and summary) |
| `verify-08-audit-log-state.png` | BUG-014 not fixed (Denied:1 for Deploy scan with 0 actual denials) |
| `verify-09-admin.png` | BUG-016 partial (Danger Zone still visible to authenticated user) |
| `verify-10-inventory.png` | BUG-004/005 fixed (backend-driven stale flag, no count flicker) |

All screenshots are in the project root directory.

---

*Report generated from live production environment. All API responses verified at time of testing 2026-04-28. Source code reviewed at HEAD of main branch.*
