# OSS Readiness Audit — SentinelLayer

**Audited:** 2026-04-09  
**Implemented:** 2026-04-10  
**Status:** All actionable items complete — H1 documented as known limitation  
**Purpose:** Issues to resolve before public OSS release

---

## CRITICAL — Blocks launch

| # | File | Line | Status | Issue |
|---|------|------|--------|-------|
| C1 | `src/api/dashboard_api.py` | 2814 | ✅ Fixed | `POST /api/admin/reset` now raises 403 when `USE_LOCAL_MOCKS=false`. |
| C2 | `src/api/dashboard_api.py` | — | ✅ Fixed | `API_KEY` env var gates all mutating POST/PATCH endpoints via `X-API-Key` header (timing-safe `secrets.compare_digest`). GET endpoints stay open. `/api/alert-trigger` exempt (has its own `ALERT_WEBHOOK_SECRET`). |
| C3 | `src/api/dashboard_api.py` | — | ✅ Fixed | `_validate_reviewed_by()` rejects empty strings and the `"dashboard-user"` generic default in live mode. Applied to all 5 HITL execution endpoints. |

---

## HIGH — Fix before significant adoption

| # | File | Line | Status | Issue |
|---|------|------|--------|-------|
| H1 | `src/api/dashboard_api.py` | 250, 287 | 📋 Known limitation | `_scans`, `_alerts`, SSE queues are in-memory. Sticky sessions (`affinity=sticky`) handle multi-replica SSE. The lifespan handler marks orphaned running scans as errors on restart. Completed scan data persists in Cosmos. Active in-progress scans are lost on pod crash and must be re-triggered. For true HA, replace with Redis pub/sub. |
| H2 | `src/api/dashboard_api.py` | ~79 | ✅ Fixed | In-memory sliding-window rate limiter added — 10 req/60s per IP on all scan and alert-trigger endpoints. |
| H4 | `src/api/dashboard_api.py` | ~1650 | ✅ Fixed | `/api/alert-trigger` now checks `Authorization: Bearer <secret>` when `ALERT_WEBHOOK_SECRET` env var is set. |

---

## MEDIUM — Fix for production grade

| # | File | Line | Status | Issue |
|---|------|------|--------|-------|
| M2 | `infrastructure/terraform-core/main.tf` | 72–77 | ✅ Fixed | `name_prefix` and `acr_prefix` now derived from `var.project_name` (default `"ruriskry"`). |
| M3 | `src/core/terraform_pr_generator.py` | 96 | ✅ Fixed | Branch prefix now reads `settings.pr_branch_prefix` (env: `PR_BRANCH_PREFIX`, default `"ruriskry/approved"`). |
| M5 | `infrastructure/terraform-core/variables.tf` | 183 | ✅ Fixed | `cosmos_free_tier` default changed to `false`. Description updated with hard warning. |
| M6 | `src/api/dashboard_api.py` | 980, 1764, 2163 | ✅ Fixed | `offset: int = 0` added to evaluations, alerts, and scan-history endpoints. Tracker `get_recent()` methods updated accordingly. |
| M8 | `src/config.py` | 84 | ✅ Fixed | `use_local_mocks` defaults to `False`. Startup logs a prominent `⚠ MOCK MODE ACTIVE` warning when true. |
| M9 | `infrastructure/terraform-core/main.tf` | 521, 899 | ✅ Fixed | `var.acr_sku` and `var.swa_sku` added to variables.tf. main.tf uses them instead of hardcoded values. |

---

## LOW — Polish for OSS credibility

| # | File | Line | Status | Issue |
|---|------|------|--------|-------|
| L1 | `src/infrastructure/inventory_builder.py` | 10 | ✅ Fixed | Docstring now uses `"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"` placeholder. |
| L2 | `src/api/dashboard_api.py` | 2800 | ✅ Fixed | Root health response uses `settings.service_name` (env: `SERVICE_NAME`, default `"ruriskry-backend"`). |
| L3 | `infrastructure/terraform-core/variables.tf` | 72 | ✅ Fixed | `search_sku` description now includes a DEV-ONLY warning about the 50 MB limit. |
| L4 | `src/api/dashboard_api.py` | — | ✅ Fixed | `_RequestIDMiddleware` assigns a UUID per request (echoes caller-supplied `X-Request-ID`), sets a `ContextVar`, injects it into all log records via `_RequestIDLogFilter`, and returns it in the response `X-Request-ID` header. |

---

## Proposed Fix Batches

### Batch A — Quick wins, no design decision needed (est. 1–2 hours)
- C1: Guard `admin/reset` — refuse in live mode (`use_local_mocks=false`)
- M5: Set `cosmos_free_tier = false` as default in variables.tf
- M6: Add `offset: int = 0` pagination param to list endpoints
- M8: Default `use_local_mocks = False` + add loud startup log warning in mock mode
- L1: Fix docstring UUID placeholder
- L2: Make health response service name use a config value

### Batch B — Naming / configuration (est. 1 hour)
- M2: Make terraform `name_prefix` derive from `var.project_name`
- M3: Make PR branch prefix configurable (`settings.pr_branch_prefix`)
- L3: Document Search free SKU limitation

### Batch C — Rate limiting (est. 30 min)
- H2: Add `slowapi` rate limiter on `/api/scan` and `/api/alert-trigger`

### Batch D — Auth (design decision required)
- C2: API authentication strategy — static API key / Azure AD / document as "deploy behind APIM"
- C3: Tie `reviewed_by` to auth identity once decided
- H4: Alert webhook signature verification (Azure Monitor Secure Webhook)

### Batch E — State durability (larger effort)
- H1: Move active scan state (`_scans`, SSE queues) to Cosmos or Redis to eliminate sticky sessions requirement

---

## Notes
- `.gitignore` already covers all `*.tfstate`, `backend.hcl`, `.terraform/` — no secrets in git
- `USE_LOCAL_MOCKS=false` hardcoded in terraform-core Container App env vars — live mode by default in deployed instances
- All Cosmos clients have proper fallbacks — no hardcoded connection strings in source
- Alert wiring already updated to use Alert Processing Rules (APR) — one rule covers all current and future alert rules in target subscription
