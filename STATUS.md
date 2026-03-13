# RuriSkry — Implementation Status

> **Read this first** if you are an AI assistant (Claude, Codex, Gemini, etc.)
> picking up this project. It tells you exactly what is done, what is live,
> and what comes next. Architecture and coding standards are in `CONTEXT.md`.

**Last updated:** 2026-03-14 (Two production bug fixes: (1) **Alert spam / deduplication cooldown** — `POST /api/alert-trigger` duplicate check now suppresses new alerts for the same resource+metric for 30 minutes after one resolves; previously the check only blocked while status=firing/investigating, so Azure Monitor's 5-minute evaluation cycle could create a new alert every 5 minutes during a persistent outage (7+ entries for the same VM); `timedelta` added to `datetime` import; (2) **Write tool exception handling** — all Azure SDK write tools in `ExecutionAgent` (`_execute_with_framework` + `_rollback_with_framework`) now wrap their SDK calls in try/except and return structured error JSON `{"success": false, "error": "..."}` instead of propagating unhandled exceptions to the `@af.tool` decorator (which returned the generic "Function failed" string to the LLM, causing misleading error messages and preventing the LLM from knowing what went wrong); affects `start_vm`, `restart_vm`, `resize_vm`, `delete_nsg_rule`, `create_nsg_rule`, `delete_resource`, `update_resource_tags` in execute phase and `start_vm`, `deallocate_vm`, `resize_vm`, `create_nsg_rule` in rollback phase; **792 tests passing, 0 failed**)

**Last updated (prev):** 2026-03-14 (Slack enterprise hardening: `slack_notifier.py` full rewrite — shared `httpx.AsyncClient` singleton (TLS connection reuse), rate limiter (`asyncio.Lock` + 1.1s min interval), smart retry (4xx=no retry, 429=Retry-After header, 5xx=exponential backoff 2s→4s, 3 max attempts), structured logging with `extra={}` on all calls for Azure Monitor queryability, one-time localhost URL warning, `notification_type` param for log traceability, resource names truncated to 100 chars; `settings.slack_timeout` made configurable (default 10s); test suite expanded 5→16 tests (5 verdict, 2 alert, 4 resolved, 4 retry behaviour, 1 localhost warning); `autouse` fixture resets all module-level state between tests; **792 tests passing, 0 failed**)

**Last updated (prev):** 2026-03-13 (Teams→Slack migration complete + `/health` endpoint added + `setup_env.sh` remote state fix: (1) `src/notifications/teams_notifier.py` deleted — replaced by `slack_notifier.py` (already existed); `src/config.py` `teams_webhook_url`/`teams_notifications_enabled` removed; all Teams references replaced with Slack across all docs; (2) `dashboard/src/App.jsx` active bug fixed — was checking `teamsStatus?.teams_configured` (always falsy since API returns `slack_configured`); renamed all state vars to `slackStatus`/`slackBtnLabel`; header now correctly shows "Slack Connected"/"Slack: Off"; (3) `GET /health` added to `dashboard_api.py` — deploy.sh referenced it but it was missing; (4) `scripts/setup_env.sh` remote state check fixed — old check looked for local `terraform.tfstate` (fails with Azure Blob backend); now uses `terraform output -raw foundry_endpoint`; (5) `tests/test_slack_notifier.py` (renamed from `test_teams_notification.py`) — 4 dead Teams tests removed, 4 new Slack tests added; (6) `docs/slack-setup.md` created — full 5-step contributor guide; dashboard redeployed; all 7 pages verified clean with Playwright; Slack webhook confirmed: `{"status":"sent"}`; **781 tests passing, 0 failed**)

**Last updated (prev):** 2026-03-13 (Two UX/correctness fixes: (1) **Terminal animation** — `handleAgentFixExecute()` in both `Alerts.jsx` and `EvaluationDrilldown.jsx` now fires a `setInterval` (2s cadence) while the `executeAgentFix()` API call is pending, appending live progress lines ("Requesting execution plan from AI...", "Waiting for Azure control plane response...", etc.) so the terminal feels interactive during the 30–60s LLM+SDK wait; interval cleared before real step animation starts; (2) **Rollback status correctness** — `ExecutionGateway.rollback_agent_fix()` was unconditionally setting `status = rolled_back` before checking `result["success"]`; fixed to only set `rolled_back` when rollback succeeds; on failure, status stays `applied` (fix is still in place) and a rose-colored "Rollback attempted but failed" banner + failed step log is shown in both `Alerts.jsx` and `EvaluationDrilldown.jsx`; rollback tests in `test_execution_gateway.py` fixed to use `ActionType.RESTART_SERVICE` (auto-rollbackable) and inject `applied` state directly to isolate from `execute_agent_fix` LLM dependency; **779 tests passing**; backend revision `--0000019` + dashboard deployed)

**Last updated (prev):** 2026-03-13 (Bug fix — cross-agent scan contamination: `get_unresolved_proposals()` returned `manual_required` records from ALL agents; when a cost scan ran and the Advisor API returned 0 proposals, the re-flagging loop in `_run_agent_scan()` was injecting unresolved monitoring-agent proposals into the cost scan's `proposals` list — those then got evaluated and stored with `agent_id: "monitoring-agent"` in the cost scan record, causing cost verdicts to be invisible in the Decisions tab; fix: added agent-id filter immediately after `get_unresolved_proposals()` call (`current_agent_id = _AGENT_REGISTRY_NAMES.get(agent_type)` + list comprehension); 2 regression tests added; **779 tests passing**; backend deployed)

**Last updated (prev):** 2026-03-13 (LLM timeout + capacity fix — scan quality restored: root cause of 0-proposal deploys scans was NOT model quality but (1) **LLM_TIMEOUT=120s too short** for gpt-5-mini's comprehensive multi-step agent loops — a 7-domain security audit makes 15-20 tool calls and needs ~10 min; increased to 600s; (2) **100K TPM hitting 429** — increased Foundry capacity from 100 to 200 (200K TPM, 200 RPM); after fix: deploy scan completed in ~11 min with **18 proposals** (vs 12 with gpt-4.1) — gpt-5-mini is MORE thorough, finding Cosmos DB public access, Key Vault purge protection gaps, storage TLS 1.0, VM password auth, activity log audit gaps; `config.py` default updated `llm_timeout: 600`; `variables.tf` default updated; **777 tests passing**; backend revision `--0000016`)

**Last updated (prev):** 2026-03-13 (Enterprise agent prompt validation — all 3 operational agents audited and fixed for enterprise readiness: (1) **Monitoring agent user prompt** expanded from VM-only (4 sentences) to full 6-step enumeration matching `_SCAN_INSTRUCTIONS` — databases/failover, Container Apps/replicas, AMA extension observability gaps, orphaned resources, Resource Health + Advisor calls; same bug as deploy/cost agents where LLM followed narrow user prompt and skipped system instruction steps; (2) **Cost agent user prompt** expanded from 6 to 8 steps — added PaaS/database rightsizing (AKS node CPU, App Service CpuPercentage, SQL DTU, Cosmos RU/s vs TotalRequests) and storage account waste; (3) **Deploy agent tool fix** — `propose_action` fallback `resource_type` changed from `_NSG_RESOURCE_TYPE` to `"unknown"` — previously, non-NSG findings (Key Vault, storage) would be incorrectly typed as NSG resources; `get_resource_details` tool description expanded with security-relevant property names (encryption, auth config, publicNetworkAccess, enableSoftDelete, enablePurgeProtection, powerState); **777 tests passing**; backend deployed revision `--0000015`)

**Last updated (prev):** 2026-03-13 (gpt-5-mini migration + agent prompt fixes + rate limit hardening: (1) **Model migration** — `gpt-4.1` → `gpt-5-mini` (version `2025-08-07`, GlobalStandard); `terraform.tfvars` + `variables.tf` updated; capacity raised 50 → 100 (50K → 100K TPM); `AZURE_OPENAI_DEPLOYMENT=gpt-5-mini` applied to Container App via targeted `terraform apply`; backend now running revision `--0000014`; (2) **Deploy agent prompt fix** — user prompt rewritten to enumerate all 7 security domains explicitly (NSG, storage, DB/KV, VM posture, activity log, tags) — previously narrow NSG-only prompt caused LLM to skip domains and return 0 proposals; (3) **Cost agent prompt fix** — user prompt rewritten to enumerate 6 cost domains explicitly (deallocated VMs, unattached disks, orphaned IPs, CPU utilisation, Advisor recommendations); (4) **LLM rate limit backoff fix** — `base_delay` in `llm_throttle.py` changed `2.0 s` → `10.0 s` (retries at 10s/20s/40s = 70s total) — old 14s total fired all retries inside Azure's 60s rate-limit window; (5) **AgentControls.jsx error display fix** — component now reads `scan_error` field (was reading wrong `.error` field); 429 errors show human-readable "Rate limited — wait 60s and retry"; (6) **Playwright perf benchmark** — `dashboard/tests/perf-benchmark.spec.js` + `playwright.perf.config.js` created: 6 API latency tests (p50/p95/max/avg, 5 samples each), 5 page load tests, 1 full scan pipeline test; results saved to `tests/results/perf-{label}-{ts}.json`; **777 tests passing**; frontend deployed)

**Last updated (prev):** 2026-03-13 (Agent intelligence architecture fix — three architectural gaps identified and closed: (1) **VM power state** — `get_resource_details_async` now calls Compute instance view API to inject `powerState` field (e.g. "VM deallocated") — previously returned static Resource Graph config only (`provisioningState: Succeeded`) so deallocated VMs appeared healthy; (2) **Azure Resource Health API** — new `get_resource_health_async` function + `get_resource_health` tool registered in all 3 agents — returns Azure Platform's own availability signal (Available/Unavailable/Degraded/Unknown); (3) **Azure Advisor API** — new `list_advisor_recommendations_async` function + `list_advisor_recommendations` tool registered in all 3 agents — exposes Microsoft pre-computed intelligence (Cost/Security/HA/Performance); added `azure-mgmt-resourcehealth>=1.0.0b1` + `azure-mgmt-advisor>=9.0.0` to requirements.txt; all 3 agents now have 8 tools each (previously 4–6, uneven); **777 tests passing**; backend deployed as revision `--0000011`)

**Last updated (prev):** 2026-03-13 (Agent intelligence overhaul — all 3 operational agent system prompts completely rewritten for enterprise-grade coverage: **MonitoringAgent** `_SCAN_INSTRUCTIONS` now 6-step scan (VM power state via `get_resource_details`, DB health, Container Apps/App Services, observability gaps including AMA extension, orphaned disks/public IPs); `_ALERT_INSTRUCTIONS` expanded from 2 to 5 alert types (availability/heartbeat, CPU/memory, disk/storage, database, network/connectivity); **root cause fix for vm-web-01 false-clean bug** — old instructions checked CPU metrics only; deallocated VMs return no metrics, which the old agent interpreted as "all clear" instead of as confirmation of down state; new instructions explicitly mandate checking VM power state first before any metric queries; **DeployAgent** `_AGENT_INSTRUCTIONS` expanded from NSG-only to 7 security domains: resource discovery (NSGs/VMs/storage/DBs/Key Vaults/public IPs), NSG audit, storage security (publicBlobAccess/HTTPS/TLS), DB & Key Vault security (publicNetworkAccess/private endpoints/soft-delete), VM security posture (disk encryption/auth type/public IP without NSG), recent config changes via activity log, zero-tag governance; **CostAgent** `_AGENT_INSTRUCTIONS` gains deallocated VM waste detection, unattached disk flagging (`diskState=Unattached` → MEDIUM delete_resource), orphaned public IP flagging (LOW), Redis/storage in discovery query, urgency scale (MEDIUM/LOW) added; **777 tests passing**)

**Last updated (prev):** 2026-03-13 (Phase 30 complete + doc sync — alert labels "Resolved"→"Investigated" (display only, stored value unchanged); Overview AlertsCard "Resolution Rate"→"Investigation Rate"; Decisions table new Agent column with colored badge pills (Monitoring=blue, Cost=amber, Deploy=purple); `initialAgent` prop on DecisionTable for URL-driven pre-selection; Scans verdict count now navigates to `/decisions?agent=<id>`; `key={agentParam}` on DecisionTable forces remount on agent filter change; all "SRE" labels renamed to "Monitoring" across AgentControls/DecisionTable/LiveLogPanel/AuditLog/Overview/Scans; dedup `action_id` update fix — existing `manual_required` record's `action_id` now updated to latest verdict's `action_id` on re-scan (prevents "No execution record" for re-scanned resources); terraform-prod AMA identity fix — both VMs (`vm-dr-01`, `vm-web-01`) given `SystemAssigned` MI + `azurerm_role_assignment` for "Monitoring Metrics Publisher" role to fix silent telemetry drop; **777 tests passing**)

**Last updated (prev):** 2026-03-12 (Phase 30 — Rollback for Agent-Applied Fixes: `Rollback` button appears next to `Applied` badge in both `EvaluationDrilldown.jsx` and `Alerts.jsx` — only when status is `applied` (agent-executed fix); confirm dialog shows `rollback_hint` from stored `execution_plan`; `ExecutionAgent.rollback()` public method with `_rollback_mock()` (deterministic inverse: RESTART→deallocate, SCALE_UP/DOWN→resize back, NSG→restore rule, DELETE→cannot auto-rollback) and `_rollback_with_framework()` (LLM-driven with write tools + `_ROLLBACK_INSTRUCTIONS`); `ExecutionGateway.rollback_agent_fix()` validates status=applied, calls `agent.rollback()`, sets status→`rolled_back`, stores `rollback_log`; `POST /api/execution/{id}/rollback` endpoint; `ExecutionRecord.rolled_back` status + `rollback_log` field; `rolled_back` badge (amber) in both status configs; `ExecutionLogView` accepts `label` prop for rollback steps display; 13 new tests; **776 tests passing**; backend + dashboard deployed)

**Last updated (prev):** 2026-03-12 (Phase 29 — Post-Execution Verification, Execution Log UI, Execution Metrics, Admin Panel, Alerts Overview Card: `ExecutionAgent.verify()` re-queries resource state after execution to confirm fix applied (`_verify_mock()` deterministic, `_verify_with_framework()` LLM-driven); `verification: {confirmed, message, checked_at}` stored on `ExecutionRecord`; `ExecutionGateway.list_all()` returns all records newest-first; `GET /api/metrics` gains `executions` block (total/applied/failed/pr_created/dismissed/pending/agent_fix_rate/success_rate); `GET /api/config` returns safe system config (mode/llm_timeout/concurrency/gateway_enabled/live_topology/version); `Admin.jsx` new page — System Configuration + Danger Zone (reset moved here from header); Settings gear + Admin link in Sidebar bottom; `AlertsCard` + `ExecutionMetricsCard` added to Overview between metric cards and SRI trend; `ExecutionLogView` in both `EvaluationDrilldown.jsx` and `Alerts.jsx` shows per-step log + verification badge after fix runs; 15 new tests; **763 tests passing**)

**Last updated (prev):** 2026-03-12 (Phase 28 — LLM-Driven Execution Agent: replaced hardcoded execution switch with `ExecutionAgent` class that dynamically reasons about HOW to implement any approved action; two-phase: `plan()` (LLM reads resource state, outputs structured steps) → human reviews → `execute()` (LLM calls Azure SDK write tools step-by-step); `ExecutionAgent.plan()` returns `{steps, summary, estimated_impact, rollback_hint, commands}` for all 7 ActionType values in mock mode; `ExecutionAgent.execute()` returns `{success, steps_completed, summary}` with fail-stop semantics; `execution_gateway.py` refactored — `generate_agent_fix_plan()` (async, replaces sync `generate_agent_fix_commands()`); `ExecutionRecord` gains `execution_plan` + `execution_log` fields; API endpoint `GET /api/execution/{id}/agent-fix-preview` now returns rich plan; `AgentFixPlanView` component renders structured plan (steps table, impact, rollback, expandable CLI) in both `EvaluationDrilldown.jsx` and `Alerts.jsx`; 22 new tests; **748 tests passing**)

**Last updated (prev):** 2026-03-12 (MonitoringAgent + alert UX fixes: (1) `_ALERT_INSTRUCTIONS` updated — new explicit guidance for heartbeat/availability alerts: stopped/deallocated VMs have no queryable metrics, empty metric results confirm the alert, agent should call `get_resource_details` + `query_activity_log` then propose `restart_service`; (2) metric field in `_normalize_azure_alert_payload()` now prefers human-readable `essentials.alertRule` over raw KQL `SearchQuery` for log alerts; (3) `description` field added to normalizer output so it flows into `alert_payload` and dashboard; (4) AlertPanel "no findings" message improved with explanatory text; (5) AlertPanel Alert Reference section now shows Value/Threshold row and Description field; backend + frontend deployed; **732 tests passing**)

**Last updated (prev):** 2026-03-12 (Alert infrastructure wiring + payload normalizer fixes — `_normalize_azure_alert_payload()` added to `dashboard_api.py`: handles Azure Monitor Common Alert Schema, non-common schema, and flat pass-through; workspace pivot: when Azure Monitor reports a Log Analytics workspace as target (Log Alerts V2 always does this), regex-extracts the actual affected VM name from `essentials.description` or `alertRule` name and constructs correct VM ARM ID — eliminates LLM non-determinism that caused ~50% of alerts to show 0 findings; `infrastructure/terraform-prod/main.tf`: `use_common_alert_schema = false` on webhook receiver; `infrastructure/terraform-prod/terraform.tfvars`: `alert_webhook_url` set to backend URL — activates `dynamic webhook_receiver` in `ag-ruriskry-prod` action group; both `alert-vm-dr-01-heartbeat` and `alert-vm-web-01-cpu-high` rules now wire to governance engine via single action group update; `deploy.md` wiring section rewritten to use `terraform-prod` approach; **732 tests passing**; backend revision `r202603120006` deployed)

**Last updated (prev):** 2026-03-11 (Dedicated Alerts tab — Azure Monitor alert investigations now visible on dashboard; `POST /api/alert-trigger` refactored to async (returns immediately with `{status: "firing", alert_id}`, investigation runs in background via `BackgroundTasks`); `AlertTracker` persistence class (`data/alerts/` mock, Cosmos `governance-alerts` live); 4 new endpoints: `GET /api/alerts` (list), `GET /api/alerts/active-count`, `GET /api/alerts/{id}/status`, `GET /api/alerts/{id}/stream` (SSE); `Alerts.jsx` page with table, severity/status filters, search, drilldown panel (timeline, outcome summary, agent findings with SRI/violations); Sidebar `Zap` icon with red active-count badge; duplicate alert detection (same resource+metric); admin reset clears `data/alerts/`; 4 new tests; **732 tests passing**; backend revision `r202603111949` + dashboard deployed)

**Last updated (prev):** 2026-03-11 (`scanned_resources` field added to scan records — `_snapshot_scanned_resources()` helper snapshots all Azure resources in scope at scan start via `ResourceGraphClient.list_all_async()`; stored as `scanned_resources: [{id, name, type, location}]` in every scan record; `list_scan_history` endpoint now returns `scanned_resources` + `scanned_resources_count`; Audit Log drilldown now shows ALL resources the agent examined — clean resources show green "Clean" badge, flagged resources show amber verdict/SRI/violations; `flaggedByName` useMemo matches by both full ARM ID and short name (lowercase); backward-compatible — old records without `scanned_resources` fall back to proposals-only display; backend + dashboard deployed as revision `r202603111905`; **728 tests passing**)

**Last updated (prev):** 2026-03-11 (Audit Log redesigned as scan-level operational audit: `AuditLog.jsx` rebuilt — one row per scan run (not per verdict); shows agent, timestamp, status icon, resources examined count, duration, outcome breakdown (✓✗⚠ counts); click row → drilldown panel lists every resource the agent examined in that scan with its verdict, SRI score, policy violations, and agent finding; filter by agent type, status, date range, free-text; export to CSV/JSON; `ScanRunTracker.get_recent(limit)` added (mock: JSON sort+slice, live: `SELECT TOP N ... ORDER BY started_at DESC`); `GET /api/scan-history` endpoint added (returns `{count, scans[]}`, strips Cosmos internal fields, adds computed counts); `fetchScanHistory()` added to `api.js`; `scans` state added to `App.jsx` (fetched in parallel with evaluations/metrics); Decisions tab continues to show governance-verdict-level data; **728 tests passing**)

**Last updated (prev):** 2026-03-11 (Two fixes: (1) AuthorizationFailed on "Execute via Agent" NSG delete — Container App MI was missing `Network Contributor` role; added `azurerm_role_assignment.network_contributor` at subscription scope in `main.tf`; targeted apply applied it; (2) Audit Log clickable rows — click any row → slide-in details panel showing: full resource ARM ID (copyable), "What the Agent Found" (full action reason), SRI™ breakdown bars (infrastructure/policy/historical/financial), policy violations list, governance rationale, triage tier/mode, audit reference IDs; `ChevronRight` indicator on each row; backdrop click closes panel; active row highlighted with blue ring)

**Last updated (prev):** 2026-03-11 (PR generator fix: root cause of stub-file fallback identified and resolved — (1) code: `_apply_nsg_fix_to_content()` Pass 2 now handles `security_rule` blocks where `{` is on the next line; better logging added to both `_find_and_patch_tf_file()` and `_apply_nsg_fix_to_content()` so failures are diagnosed immediately in Container App logs; 9 new unit tests covering standalone resource, inline block, multi-rule NSG, already-Deny, brace-on-next-line; Docker image rebuilt + pushed; new Container App revision `r202603111556` deployed; E2E test passes in 56s; (2) root cause in test env: live Azure NSG rule is named `ssh` but IaC test repo has `allow-ssh-anywhere` — name mismatch causes `_apply_nsg_fix_to_content()` to return None → stub fallback; fix: update `psc0des/ruriskry-iac-test/infrastructure/terraform-prod/main.tf` to rename `security_rule.name` from `allow-ssh-anywhere` to `ssh`; **728 tests passing**)

**Last updated (prev):** 2026-03-11 (Production debugging session — 4 bugs found and fixed via E2E Playwright testing: (1) `src/infrastructure/search_client.py` — catches `HttpResponseError 404` when `incident-history` index missing on fresh deploy; returns `[]` instead of crashing scan; (2) `src/core/scan_run_tracker.py` — `get_latest_completed_by_agent_type()` now includes `status='error'` alongside `status='complete'` — previously error scans were invisible in last-run and scan history; (3) `terraform.tfvars` — `iac_github_repo` typo fixed: `psc0codes` → `psc0des`; every Execution Gateway PR creation was returning GitHub 404; applied via targeted terraform apply; (4) `dashboard/tests/scan-capture.spec.js` — new E2E test: triggers deploy scan, polls to completion, verifies last-run endpoint captures it, checks scan history table shows the row, checks decisions table shows verdict, clicks drilldown and asserts execution status is not Failed; also fixed Container App revision force-update pattern: `--revision-suffix` required to pull new `latest` image when tag hasn't changed)

**Last updated (prev):** 2026-03-11 (Fix: agent scans failing with 401 PermissionDenied on Foundry — `local_authentication_enabled=false` on the Foundry AIServices account disables API key auth; Container App MI was missing `Cognitive Services OpenAI User` role; added `azurerm_role_assignment.foundry_openai_user` in `main.tf`; added `lifecycle { ignore_changes = [access_policy] }` on KV to eliminate perpetual drift from mixed inline+standalone access policy pattern; `deploy.md` Known Gotchas updated; `docs/ARCHITECTURE.md` + `docs/SETUP.md` updated with role requirement)

**Last updated (prev):** 2026-03-11 (E2E Playwright testing of live production dashboard — 2 bugs found and fixed: (1) `dashboard/public/staticwebapp.config.json` created with `navigationFallback` — SPA routing fix so direct URLs like `/overview` return 200 instead of 404 on Azure Static Web Apps; (2) `dashboard/src/pages/Scans.jsx` — `no_data` filter added (same fix already applied to Overview.jsx) — eliminates phantom scan rows when agents have no history; all 5 pages verified clean: Overview, Scans, Agents, Decisions, Audit Log; screenshots saved to `screenshots/e2e-*.png`)

**Last updated (prev):** 2026-03-11 (Deploy hardening continued: RG renamed `ruriskry-core-rg` → `ruriskry-core-engine-rg` across all files; `enable_rg_lock` variable added (default `false`) — eliminates `ScopeLocked 409` on redeploys; `purge_protection_enabled=false` + `soft_delete_retention_days=7` — eliminates KV soft-delete 409 on fresh redeploys; `GITHUB_PAT` env var support in deploy.sh — non-interactive CI/CD deploys; deploy.md fully reordered for newcomer clarity: One-time Setup section, correct step ordering, state lock gotcha added; `docs/ARCHITECTURE.md` KV security row updated)

**Last updated (prev):** 2026-03-10 (Placeholder image pattern: Container App now starts with `mcr.microsoft.com/k8se/quickstart:latest` (public MCR, no ACR auth); `deploy.sh` swaps to real ACR image via `az containerapp update --image` after Stage 2 when AcrPull role is guaranteed propagated; eliminates "unable to pull image using Managed identity" race condition; `time_sleep.acr_role_propagation` removed from Terraform; `lifecycle { ignore_changes }` on container image; ARM role propagation poll removed from deploy.sh; deploy.md Manual Deploy updated with Step 4a; Known Gotchas updated)

**Last updated (prev):** 2026-03-10 (GitHub PAT automation in deploy.sh: script now prompts for PAT interactively when `use_github_pat = true` and `github-pat` is absent from Key Vault; stores it via `az keyvault secret set`; forces Container App restart via `az containerapp update`; if user skips, patches `use_github_pat = false` in tfvars and does targeted Container App re-apply; secret already present → silent skip; `deploy.md` "Store GitHub PAT" section updated; `docs/SETUP.md` Step 3 updated to explain KV vs local `.env` distinction)

**Last updated (prev):** 2026-03-10 (Deploy script hardening: full `repo:tag` parsing from `backend_image` var — no longer hardcodes `:latest`; `--stage2` ACR image existence check before apply; Docker daemon check skipped in `--stage2`; `terraform init` by default + `--upgrade-providers` flag; `npm ci` instead of `npm install` in deploy.sh and deploy.md; health check warn-not-die policy; `RG_NAME` from `terraform output` not hardcoded; `terraform fmt` applied — all three files pass `fmt -check`; stale `dashboard_url` removed from `terraform.tfvars`; deploy.md Manual Deploy updated: `terraform init` + `npm ci`; `learning/52-deploy-hardening.md` created)

**Last updated (prev):** 2026-03-10 (Terraform-native DASHBOARD_URL wiring: removed `var.dashboard_url` variable and Python tfvars-patching from `deploy.sh`; Container App now references `azurerm_static_web_app.dashboard.default_host_name` directly — Terraform creates SWA first (implicit dependency), reads the URL in-memory, and passes it into `DASHBOARD_URL` in the same apply; SWA removed from Stage 1 targets; Step 6 "Wire dashboard URL back" deleted from Manual Deploy path; `acr_admin_username` + `acr_admin_password` dead outputs removed (`admin_enabled = false`); `next_steps` heredoc rewritten to show live URLs; stale comment "Secrets (ACR password)" corrected in `main.tf`; `--stage2` guard added; health check retries 3× with 15s delay for Container App cold start)

**Last updated (prev):** 2026-03-10 (Production deployment hardening: `scripts/deploy.sh` one-command deploy — staged apply (ACR + User-Assigned MI + AcrPull role + 90s sleep → docker build/push → full apply → dashboard → tfvars wiring); `--stage2` flag for resuming after Stage 1; image-exists detection skips redundant Docker rebuild; cross-platform python3/python detection; `npx` prerequisite check; `azurerm_user_assigned_identity.acr_pull` fixes `Operation expired` — AcrPull granted before Container App exists, no chicken-and-egg race; `azurerm_management_lock` `depends_on` all major resources — `terraform destroy` removes lock first automatically, no manual `az lock delete` needed; removed `terraform_data.docker_push` provisioner (was fragile, trigger-based); `terraform.tfvars.example` updated with correct defaults (`ruriskry-core-rg`, `create_foundry_project=true`, `foundry_capacity=50`, placeholder `iac_github_repo`); `deploy.md` fully rewritten with prerequisites table, Windows/Git Bash callout, failure recovery table, `--stage2` guidance; Docker Desktop now required (local build replaces ACR Tasks); provider lock files committed for both terraform-core and terraform-prod)

**Last updated (prev):** 2026-03-08 (Deployment infra: `infrastructure/terraform` renamed to `infrastructure/terraform-core`; ACR + Container Apps Environment + Container App + Static Web App resources added to `terraform-core/main.tf`; new variables (backend_image, backend_cpu/memory, backend_min/max_replicas, execution_gateway_enabled, llm_timeout, llm_concurrency_limit, slack_webhook_url, dashboard_url, org_name/compliance/risk_tolerance, static_web_app_location); new outputs (acr_login_server, acr_name, backend_url, dashboard_url, dashboard_deployment_token); `Dockerfile` + `.dockerignore` created at repo root; `docs/ARCHITECTURE.md` + `docs/SETUP.md` updated with deployment architecture and deploy commands; all references to old `infrastructure/terraform/` path updated)

**Last updated (prev):** 2026-03-08 (Demo cleanup: Triage Intelligence card hidden in `Overview.jsx` (`{false && metrics && ...}`) — card will be re-enabled post-hackathon. Triage backend (Phases 26/27A) remains fully functional. README.md cleaned of triage mentions for public presentation. Internal dev docs (CONTEXT.md, ARCHITECTURE.md, SETUP.md, API.md) kept accurate since code still exists.)

**Last updated (prev):** 2026-03-08 (Dashboard: Triage Intelligence card added to `Overview.jsx` — teal GlowCard showing LLM calls saved (NumberTicker), Tier 1/2/3 counts with percentages, stacked progress bar (emerald/amber/rose); `Cpu` icon from lucide-react; `pipeline.py` block comment updated to accurately reflect Phases 26 + 27A active and Phase 27B as next. No new tests — 719 still passing.)

**Last updated (Phase 27A):** 2026-03-08 (Phase 27A: Tier 1 Short-Circuiting — `force_deterministic: bool = False` on all 4 governance agents; pipeline computes `force_deterministic = (triage_tier == 1)` and passes it through; `triage_mode` field added to `GovernanceVerdict` and stored in decision records; `/api/metrics` triage section gains `deterministic_evaluations` and `full_evaluations` counts; 16 new tests; **719 tests passing**)

**Last updated (Phase 26):** 2026-03-08 (Phase 26: Risk Triage Foundation — `ActionFingerprint` + `OrgContext` Pydantic models; `src/core/risk_triage.py` (`compute_fingerprint`, `classify_tier`, `build_org_context`); 5 new org-context config settings (`ORG_NAME`, `ORG_RESOURCE_COUNT`, `ORG_COMPLIANCE_FRAMEWORKS`, `ORG_RISK_TOLERANCE`, `ORG_BUSINESS_CRITICAL_RGS`); pipeline stamps `verdict.triage_tier`; `decision_tracker` stores `triage_tier` in every record; `/api/metrics` returns triage breakdown + LLM calls saved; 38 new tests; **704 tests passing**)

**Last updated (Phase 25):** 2026-03-08 (Phase 25: LLM timeout hardening — `llm_timeout: int = 120` config setting (env: `LLM_TIMEOUT`); `asyncio.wait_for` wrapper in `run_with_throttle` caps entire agentic loop; `timeout=float(self._cfg.llm_timeout)` on all 7 `AsyncAzureOpenAI` clients; scan errors now fail fast at 120s instead of hanging for 600s; Overview.jsx agent label lookup fixed to prefer `agent_type` over `source`; 666 tests passing | Bug fixes: scan status now `"error"` when LLM times out (not silently `"complete"`); Scans page shows red Error badge + tooltip; agent label lookup fixed to use `scan.agent_type` instead of `scan.source="scan_tracker"`; Terraform action group wired with `dynamic webhook_receiver` → `alert_webhook_url` variable so Azure Monitor alerts POST to `/api/alert-trigger`; Playwright tests fixed: metrics contract updated from `approval_rate` → `decisions`/`decision_percentages`, verdict badge locator scoped to tbody, Agents heading strict-mode fix, Duration/status column waits; e2e deploy scan test accepts framework error as valid outcome; 32 passed / 0 failed / 1 skipped) | Phase 24b: Ops Nerve Center aesthetic — DM Sans UI font + JetBrains Mono data font via Google Fonts; CSS design token system (`--bg-base`, `--accent-*`, `--font-ui`, `--font-data`); dot-grid background at opacity 0.045; teal `animate-breathe` glow on SL logo; slow amber `animate-urgent-pulse` on Pending Reviews card + `animate-icon-urgent` on Decisions sidebar icon when HITL reviews pending; `GlowCard` upgraded with `backdrop-filter: blur(12px)` glass depth; `TableSkeleton.jsx` shimmer placeholder rows; `AgentControls.jsx` emoji → Lucide SVG icons; all 5 pages verified with Playwright; 666 tests still passing) | Phase 24: Magic UI visual redesign — NumberTicker count-up metrics, GlowCard border beam scan, VerdictBadge dot+glow (emerald/amber/rose), gradient AreaChart for SRI trend, animated sidebar active indicator

**Last Phase 22H note:** 2026-03-07 (Phase 22H: Governance engine Rule 3.5 — HIGH violations now floor verdict at ESCALATED even when composite ≤ 25; fixes "score dilution" where high sri_policy was overwhelmed by low blast radius / cost / historical dims; `_detect_nsg_without_deny_all` in deploy_agent fixed to set `nsg_change_direction="restrict"`; producer contract tests in `test_deploy_agent.py`; end-to-end verdict floor test; 666 tests passing)
**Active branch:** `main`
**Demo verdict:** All 3 scenarios pass with real prod resource IDs (DENIED / APPROVED / ESCALATED)

---

## Quick State Summary

| Layer | Status | Backend |
|-------|--------|---------|
| Core models (`models.py`) | ✅ Complete | `GovernanceAdjustment` + `LLMGovernanceOutput` added (Phase 22) |
| Governance engine (SRI scoring) | ✅ Complete | Critical-violation auto-DENY respects `llm_override` field on `PolicyViolation` |
| Dangerous-port detection | ✅ Complete (Phase 22E) | POL-SEC-002 (CRITICAL) restored — fires on `nsg_change_direction="open"` only; remediation (restrict) never blocked |
| Policy agent | ✅ Complete + tested | LLM now adjusts scores via `submit_governance_decision`; remediation intent detection |
| Blast radius agent | ✅ Complete + LLM decision maker + live topology | Live: `ResourceGraphClient` (KQL + tags) + GPT-4.1 decision maker · Mock: `seed_resources.json` |
| Historical agent | ✅ Complete + LLM decision maker + live search | Azure AI Search (BM25) + GPT-4.1 decision maker |
| Financial agent | ✅ Complete + LLM decision maker + live cost | Live: `ResourceGraphClient` + Azure Retail Prices API · Mock: `seed_resources.json` |
| LLM governance utilities | ✅ Complete (Phase 22C) | `src/governance_agents/_llm_governance.py` — guardrail clamping, parse (Pydantic-validated), format (guardrail note), `annotate_violations` (CRITICAL guardrail) |
| Operational agent: deploy-agent | ✅ Complete | `data/seed_resources.json` + Resource Graph + NSG rules |
| Generic Azure tools (`azure_tools.py`) | ✅ Complete | `src/infrastructure/azure_tools.py` |
| Two-layer intelligence (Phase 12) | ✅ Complete | ops agents + GPT-4.1 investigation |
| Alert-trigger endpoint | ✅ Complete | `POST /api/alert-trigger` |
| Pipeline (parallel execution) | ✅ Complete | `asyncio.gather()` (async-first) |
| Microsoft Agent Framework | ✅ Complete | `agent-framework-core` + GPT-4.1 |
| Decision tracker | ✅ Complete | Azure Cosmos DB (live) / JSON (mock) |
| MCP server | ✅ Complete | FastMCP stdio (`server.py`) |
| Dashboard API | ✅ Complete | FastAPI REST (33 endpoints; alerts lifecycle (list/status/stream/active-count); `GET /api/scan-history` — operational scan audit; scan runs durable; SSE live log; Slack status + test; explanation engine; HITL agent fix) |
| Slack notifications (Phase 17) | ✅ Complete | `src/notifications/slack_notifier.py` — Slack Block Kit message on DENIED/ESCALATED |
| Live Azure topology (Phase 19) | ✅ Complete | `ResourceGraphClient._azure_enrich_topology()` — tag-based + KQL network topology; `cost_lookup.py` — Azure Retail Prices API; `USE_LIVE_TOPOLOGY=true` opt-in flag |
| Async end-to-end migration (Phase 20) | ✅ Complete | All 7 agents: every `@af.tool` callback `async def` (incl. historical + policy, fixed post-audit); async Azure SDK clients (`aio.*`); `asyncio.gather()` for 4 concurrent KQL queries; `aclose()` on `ResourceGraphClient`, `BlastRadiusAgent`, `FinancialImpactAgent` |
| Decision explanation engine (Phase 18) | ✅ Complete | `src/core/explanation_engine.py` — counterfactual analysis, per-dimension factors, LLM summary |
| Evaluation drilldown (Phase 18) | ✅ Complete | `EvaluationDrilldown.jsx` — 6-section full-page drilldown: SRI bars, explanation, counterfactuals, reasoning |
| Agent scan triggers (Phase 13) | ✅ Complete | POST /api/scan/cost\|monitoring\|deploy\|all + GET status |
| AgentControls dashboard panel (Phase 13) | ✅ Complete | `dashboard/src/components/AgentControls.jsx` |
| Scan run tracker (Phase 16) | ✅ Complete | `src/core/scan_run_tracker.py` — Cosmos DB / JSON; survives restarts |
| SSE live log stream (Phase 16) | ✅ Complete | `GET /api/scan/{id}/stream` — real-time event streaming |
| Agent action menus (Phase 16) | ✅ Complete | ⋮ dropdown on each ConnectedAgents card — 6 actions |
| Environment-agnosticism fixes | ✅ Complete | Broadened KQL, generic tags, `[]` fallback, mock fixes |
| Azure infrastructure (Terraform) | ✅ Deployed | Foundry · Search · Cosmos · KV |
| Mini prod environment (Terraform) | ✅ Complete | `infrastructure/terraform-prod/` |
| Secret management | ✅ Complete | Key Vault + `DefaultAzureCredential` |
| Live Azure wiring | ✅ Complete | All 3 services connected |
| React dashboard | ✅ Complete | `dashboard/` (Vite + React, same repo) |
| A2A Protocol server | ✅ Complete | `agent-framework-a2a` + `a2a-sdk` |
| A2A operational clients | ✅ Complete | `A2ACardResolver` + `A2AClient` + `httpx` |
| A2A agent registry | ✅ Complete | JSON (mock) / Cosmos DB (live) |
| Execution Gateway & HITL (Phase 21) | ✅ Complete | `src/core/execution_gateway.py` + `terraform_pr_generator.py` — IaC-safe execution via GitHub PRs; JSON-durable `ExecutionRecord`; HITL Approve/Dismiss in dashboard; live Azure tag lookup for IaC detection; agent fix via `az` CLI (preview + execute); Create PR from `manual_required`; Decline/Ignore |
| LLM-as-Decision-Maker (Phase 22) | ✅ Complete | All 4 governance agents rearchitected — LLM adjusts scores via `submit_governance_decision`; `_llm_governance.py` guardrails; remediation intent detection; 621 tests |

---

## Completed Phases (Chronological)

### Phase 1 — Core Domain Models
- [x] `src/core/models.py` — all Pydantic models: `ProposedAction`, `GovernanceVerdict`,
  `SkryRiskIndex`, `BlastRadiusResult`, `PolicyResult`, `HistoricalResult`,
  `FinancialResult`, `SimilarIncident`
- [x] `src/config.py` — SRI thresholds + dimension weights via `pydantic-settings`
- [x] Learning: `learning/01-policy-agent.md`, `learning/02-governance-engine.md`

### Phase 2 — Governance Agents
- [x] `src/governance_agents/policy_agent.py` — 6 policies, critical-violation override
- [x] `src/governance_agents/blast_radius_agent.py` — resource dependency graph traversal
- [x] `src/governance_agents/historical_agent.py` — incident similarity scoring
- [x] `src/governance_agents/financial_agent.py` — cost delta + over-optimisation detection
- [x] Full unit test suite in `tests/`
- [x] Learning: `learning/03-blast-radius.md` through `learning/05-financial-agent.md`

### Phase 3 — Pipeline + Operational Agents
- [x] `src/core/pipeline.py` — parallel evaluation (later refactored to `asyncio.gather()`)
- [x] `src/core/decision_tracker.py` — audit trail
- [x] `src/operational_agents/monitoring_agent.py` — anomaly detection + action proposals
- [x] `src/operational_agents/cost_agent.py` — idle resource detection + savings proposals
- [x] `demo.py` — 3-scenario end-to-end demo
- [x] Learning: `learning/07-operational-agents.md`, `learning/08-pipeline.md`

### Phase 4 — MCP Server + Dashboard API
- [x] `src/mcp_server/server.py` — MCP tools: `evaluate_action`, `get_recent_decisions`,
  `get_resource_risk_profile`
- [x] `src/api/dashboard_api.py` — FastAPI REST: `/evaluate`, `/decisions`, `/health`
- [x] `src/core/interception.py` — MCP action interception layer
- [x] Learning: `learning/09-mcp-server.md`, `learning/10-dashboard-api.md`,
  `learning/12-interception.md`

### Phase 5 — Azure Infrastructure (Terraform)
- [x] Terraform: `azurerm_ai_services` (Foundry) + `azurerm_cognitive_deployment` (gpt-41)
- [x] Terraform: Azure AI Search, Cosmos DB, Key Vault, Log Analytics
- [x] `scripts/setup_env.sh` — auto-populates `.env` from Terraform outputs
- [x] Learning: `learning/13-azure-infrastructure.md`, `learning/14-azure-ai-foundry.md`

### Phase 6 — Secret Management
- [x] `src/infrastructure/secrets.py` — `KeyVaultSecretResolver` (env → Key Vault → empty)
- [x] All infrastructure clients updated: env override → Key Vault → mock fallback
- [x] `.env` uses secret-name vars (`AZURE_OPENAI_API_KEY_SECRET_NAME=foundry-primary-key`)
  not plaintext keys
- [x] Learning: `learning/15-keyvault-managed-identity.md`

### Phase 7 — Live Azure Service Wiring
- [x] `src/infrastructure/openai_client.py` — added `analyze()` governance wrapper
- [x] `src/infrastructure/search_client.py` — added `index_incidents()` (idempotent seeding)
- [x] `src/core/decision_tracker.py` — delegates to `CosmosDecisionClient` (Cosmos DB live)
- [x] `blast_radius_agent.py` + `financial_agent.py` — GPT-4.1 enriches `reasoning` field
- [x] `historical_agent.py` — routes to Azure AI Search in live mode (BM25 full-text)
- [x] `scripts/seed_data.py` — 7/7 incidents indexed to `incident-history` Azure AI Search index
- [x] `demo.py` verified on live Azure: DENIED(77.0) / APPROVED(14.1) / ESCALATED(54.0)
- [x] Commit: `d9c467e` — `feat(azure): wire live Azure services with Key Vault secret resolution`
- [x] Learning: `learning/15-azure-integration.md`

### Phase 8 — Microsoft Agent Framework SDK
- [x] `requirements.txt` — added `agent-framework-core>=1.0.0rc2`
- [x] All 4 governance agents refactored: rule-based logic extracted to `_evaluate_rules()`
  and registered as `@af.tool`; GPT-4.1 (via `agent.run()`) calls the tool and synthesises reasoning
  - `blast_radius_agent.py` → tool: `evaluate_blast_radius_rules(action_json)`
  - `policy_agent.py` → tool: `evaluate_policy_rules(action_json, metadata_json)`
  - `historical_agent.py` → tool: `evaluate_historical_rules(action_json)`
  - `financial_agent.py` → tool: `evaluate_financial_rules(action_json)`
- [x] All 3 operational agents use same framework pattern:
  - `cost_agent.py` → tool: `scan_cost_opportunities()`
  - `monitoring_agent.py` → tool: `scan_anomalies()`
  - `src/operational_agents/deploy_agent.py` — **NEW**: 3 detection rules (NSG deny-all,
    lifecycle tags, sparse topology); tool: `scan_deploy_opportunities()`
- [x] `src/core/pipeline.py` — added `DeployAgent` + new `scan_operational_agents()` method
- [x] Auth pattern: `DefaultAzureCredential` + `get_bearer_token_provider` → `AsyncAzureOpenAI`
  (Responses API requires `api_version="2025-03-01-preview"`)
- [x] Mock fallback preserved in governance agents: `_use_framework = not use_local_mocks and bool(endpoint)`
  (Phase 15 changed this for ops agents only — governance agent pattern is unchanged)
- [x] Commit: `6fac593` — `feat(framework): rebuild all agents on Microsoft Agent Framework SDK`
- [x] Learning: `learning/16-microsoft-agent-framework.md`

### Phase 20 — Async End-to-End Migration  ← LATEST

**Problem:** The Microsoft Agent Framework's `FunctionTool._invoke` calls sync `@af.tool`
callbacks **directly on the event loop** — no thread pool, no executor. Every sync Azure SDK
call inside a tool blocked all 4 governance agents' `asyncio.gather()` from running in parallel,
making concurrent evaluation effectively sequential (~1,100ms per evaluation instead of ~300ms).

**Solution:** Make all `@af.tool` callbacks `async def` and use async Azure SDK variants underneath.

- `src/infrastructure/cost_lookup.py`:
  - `_extract_monthly_cost(items, os_type) -> float | None` — new module-level shared helper
    (OS-aware PAYG filtering, extracted from both sync and async code paths).
  - `get_sku_monthly_cost_async(sku, location, *, os_type="")` — async variant using
    `httpx.AsyncClient`; shares the same `_cache` dict with the sync version (GIL-safe).
- `src/infrastructure/resource_graph.py`:
  - `_async_rg_client` — `azure.mgmt.resourcegraph.aio.ResourceGraphClient` created in `__init__`.
  - `get_resource_async()`, `list_all_async()` — public async API.
  - `_azure_enrich_topology_async()` — uses `asyncio.gather(nsg_for_vm, vms_behind_nsg,
    reverse_dependents, get_cost)` so 4 KQL queries + 1 HTTP call run concurrently.
- `src/governance_agents/blast_radius_agent.py` + `financial_agent.py`:
  - `_evaluate_rules_async()`, `_find_resource_async()`, and zone/SPOF helpers all `async def`.
  - `evaluate()` non-framework path: `await self._evaluate_rules_async(action)`.
  - `@af.tool evaluate_blast_radius_rules` / `evaluate_financial_rules` → `async def`.
- `src/infrastructure/azure_tools.py`:
  - 5 async variants added: `query_resource_graph_async`, `query_metrics_async`,
    `get_resource_details_async`, `query_activity_log_async`, `list_nsg_rules_async`.
  - Each uses the corresponding `azure.*.aio.*` SDK client in live mode; same mock fallback.
- `src/operational_agents/cost_agent.py`, `monitoring_agent.py`, `deploy_agent.py`:
  - All `@af.tool` callbacks that call Azure (`query_resource_graph`, `query_metrics`, etc.)
    changed to `async def` and use `await azure_tool_async(...)` instead.
  - `propose_action` remains sync (no I/O).
- `tests/test_async_migration.py` (NEW) — 34 tests across 6 test classes:
  - `TestCostLookupAsync` (6): async cost lookup, cache sharing, transient failure non-caching.
  - `TestExtractMonthlyCost` (5): shared OS-aware helper.
  - `TestResourceGraphClientAsync` (4): async methods + `asyncio.gather` call verification.
  - `TestAsyncAzureTools` (7): async variants return same mock data as sync.
  - `TestGovernanceAgentAsyncTools` (4): `@af.tool` callbacks are `async def` (blast, financial, historical, policy).
  - `TestOpsAgentAsyncTools` (4): ops agent tools async; `propose_action` stays sync.
  - `TestAsyncHelperMethods` (9): regression guards for all new async methods/functions + `aclose()` + historical helper.
- `tests/test_live_topology.py` — updated 2 live-mode tests to use `AsyncMock` for
  `get_resource_async` (previously used sync `get_resource` mock, now fails to await).
- **Test result: 500 passed, 0 failed** ✅ (+34 new tests)

**Post-Phase-20 Audit Round 1 (testing team deep audit):**
- `resource_graph.py` + `azure_tools.py`: all async client instantiation sites changed from
  `azure.identity.DefaultAzureCredential` (sync) to `azure.identity.aio.DefaultAzureCredential`
  (async). Async SDK clients call `await credential.get_token()` — a sync credential raises
  `TypeError` on first real auth call.
- `blast_radius_agent.py:293` + `financial_agent.py:302`: framework "tool not called" fallback
  changed from `return self._evaluate_rules(action)` (sync, blocks event loop) to
  `return await self._evaluate_rules_async(action)`.
- `resource_graph.py`: added `async def aclose(self)` — closes `_async_rg_client` and releases
  the aiohttp connection pool. Call at application shutdown to silence `ResourceWarning`.
- `.gitignore`: `data/scans/` added alongside `data/decisions/` — both are runtime-generated
  directories that should never be committed.

**Post-Phase-20 Audit Round 2 (testing team follow-up):**
- `historical_agent.py`: `@af.tool evaluate_historical_rules` was still `def` (sync). In live
  mode it calls `self._search.search_incidents()` — a blocking Azure AI Search network call that
  would block the event loop. Fixed: changed to `async def`; added `_evaluate_rules_async()` that
  wraps the blocking call in `asyncio.to_thread()` in live mode; mock mode calls sync directly
  (pure computation, no I/O).
- `policy_agent.py`: `@af.tool evaluate_policy_rules` was still `def` (sync). Policy evaluation
  is pure JSON rule matching (no Azure calls), so it doesn't block the event loop, but the
  documented "all `@af.tool` callbacks `async def`" architecture contract was violated. Fixed:
  changed to `async def`.
- `blast_radius_agent.py` + `financial_agent.py`: `aclose()` was defined on `ResourceGraphClient`
  but neither agent exposed it to callers. Added `async def aclose()` to both agents, delegating
  to `self._rg_client.aclose()` when in live topology mode.
- `.gitignore`: added `.claude/` (session-local AI context, not source code).
- `tests/test_async_migration.py`: +5 tests — `TestGovernanceAgentAsyncTools` grows from 2→4
  (historical + policy); `TestAsyncHelperMethods` grows from 6→9 (aclose × 2 + historical helper).
- **Test result: 505 passed, 0 failed** ✅ (+5 new tests)

**Post-Phase-20 Audit Round 3 (testing team — low severity):**
- `azure_tools.py` — `query_resource_graph_async`, `query_metrics_async`,
  `query_activity_log_async`: credentials were instantiated inline (`ResourceGraphClient(DefaultAzureCredential())`).
  When the client's `async with` block exited, the *client's* HTTP session was closed but the
  `DefaultAzureCredential` object (which holds its own internal HTTP connections for token
  acquisition) was never explicitly closed. Fixed: each site now nests
  `async with DefaultAzureCredential() as credential:` around `async with SomeClient(credential) as client:`.
  Both are closed deterministically. No behaviour change in mock mode or test suite.
- **Test result: 505 passed, 0 failed** ✅ (no new tests needed — mock path unchanged)

### Phase 19 — Live Azure Topology for Governance Agents

**Problem:** `BlastRadiusAgent` and `FinancialImpactAgent` loaded `data/seed_resources.json`
at startup and evaluated every live action against a static snapshot. In live mode, governance
must query Azure *right now* to know actual blast radius and real monthly cost.

**Solution:**
- `src/infrastructure/cost_lookup.py` (NEW) — `get_sku_monthly_cost(sku, location)`. Queries
  the **Azure Retail Prices REST API** (public, no auth, `httpx` already installed). Converts
  min retail hourly price × 730 → monthly USD. Module-level `_cache` prevents repeated calls.
- `src/infrastructure/resource_graph.py` — added `_azure_enrich_topology(resource) -> dict`.
  Runs per-resource KQL queries to infer dependency edges from Azure directly:
  1. `depends-on` tag → `dependencies` list
  2. `governs` tag → `governs` list
  3. VM → NIC → NSG join → adds NSG to `dependencies`
  4. NSG → NIC → VM join → populates `governs` with governed VMs
  5. Reverse depends-on scan → `dependents` list
  6. `cost_lookup.get_sku_monthly_cost()` → `monthly_cost` field
  `_azure_get_resource()` and `_azure_list_all()` both call it. Every KQL query wrapped in
  `try/except` — topology failure never crashes governance.
- `src/governance_agents/blast_radius_agent.py` — `__init__` branched on
  `_live = not use_local_mocks and bool(subscription_id) and use_live_topology`:
  JSON load when not live (unchanged); live → `ResourceGraphClient(cfg)`, no JSON.
  `_find_resource()`, `_detect_spofs()`, `_get_affected_zones()` all route to `_rg_client`
  in live mode.
- `src/governance_agents/financial_agent.py` — same branch pattern. `_find_resource()` in
  live mode returns dict with `monthly_cost` already populated by `_azure_enrich_topology`.
- `infrastructure/terraform-prod/main.tf` — added `depends-on` and `governs` tags to all
  4 governed resources so live tag-based inference works immediately after `terraform apply`.
- `src/config.py` — `use_live_topology: bool = False` (env var `USE_LIVE_TOPOLOGY=true`).
  Third gate required alongside `USE_LOCAL_MOCKS=false` + `AZURE_SUBSCRIPTION_ID`. Default
  `false` prevents tests from making real Azure calls when subscription is configured.
- `.env` / `.env.example` — `USE_LIVE_TOPOLOGY=true` added in the Mock vs Azure section.
- `tests/test_live_topology.py` (NEW) — 16 tests: `TestCostLookup` (6),
  `TestResourceGraphLiveEnrichment` (4), `TestBlastRadiusAgentLiveMode` (3),
  `TestFinancialAgentLiveMode` (3). All mock Azure SDK; no credentials needed.
  (`MagicMock` cfg objects auto-return truthy for `use_live_topology` → live branch tested.)
- `tests/test_decision_tracker.py` — removed all 10 `@pytest.mark.xfail` decorators from
  `TestRecord`; fixed `tracker._dir` → `tracker._cosmos._decisions_dir` (8 occurrences).
  These tests were xfailed since Phase 7 Cosmos migration; now fully passing.
- `azure-mgmt-resourcegraph==8.0.1` installed in venv (was in requirements.txt but missing).
- **Test result: 460 passed, 0 failed** ✅ (+16 new + 10 formerly-xfailed now passing)

### Phase 18 — Decision Explanation Engine & Counterfactual Analysis

Every governance verdict now has a full explanability layer. Clicking any row in the Live
Activity Feed opens a 6-section full-page drilldown: verdict header with SRI composite,
4 weighted SRI bars (with primary-factor star), plain-English explanation, counterfactual
"what would change this?" cards, per-governance-agent reasoning, and a collapsible raw JSON
audit trail.

**`src/core/explanation_engine.py`** (NEW)
- [x] `DecisionExplainer.explain(verdict, action)` — async; returns `DecisionExplanation`.
- [x] Builds ranked `Factor` list from all 4 SRI dimensions; identifies the primary factor.
- [x] Extracts policy violations from `agent_results["policy"]`.
- [x] Generates 3 `Counterfactual` scenarios per verdict type (what would change the outcome).
- [x] `_try_llm_summary()` — GPT-4.1 natural-language summary via `run_with_throttle`; falls back to template string in mock mode.
- [x] Module-level `_explanation_cache` — keyed by `action_id`; prevents redundant re-computation on repeated API calls.

**`src/core/models.py`** — 3 new Pydantic models added
- [x] `Factor(dimension, score, weight, weighted_contribution, reasoning)` — one SRI dimension.
- [x] `Counterfactual(change_description, predicted_new_score, predicted_new_verdict, explanation)`.
- [x] `DecisionExplanation(summary, primary_factor, contributing_factors, policy_violations, risk_highlights, counterfactuals)`.

**`src/api/dashboard_api.py`** — 1 new endpoint (18 total)
- [x] `GET /api/evaluations/{id}/explanation` — looks up stored record, reconstructs `GovernanceVerdict` handling both full Pydantic format and flat DecisionTracker format, calls `DecisionExplainer.explain()`, returns `DecisionExplanation.model_dump()`.

**Frontend**
- [x] `dashboard/src/components/EvaluationDrilldown.jsx` (NEW) — 6-section full-page drilldown. Section 1: verdict header + SRI composite. Section 2: 4 horizontal SRI bars with primary-factor ⭐. Section 3: explanation summary + primary factor callout + risk highlights + policy violations. Section 4: counterfactual analysis cards with score arrows. Section 5: proposing agent reason + governance agent assessments. Section 6: collapsible full JSON audit trail.
- [x] `dashboard/src/components/LiveActivityFeed.jsx` — rows are now clickable (`cursor-pointer hover`); `onDrilldown` prop passed up to `App.jsx`.
- [x] `dashboard/src/App.jsx` — `drilldownEval` state; `EvaluationDrilldown` renders in place of main dashboard when a row is clicked; Back button returns to overview.
- [x] `dashboard/src/api.js` — `fetchExplanation(evaluationId)` helper.

**Tests**
- [x] `tests/test_explanation_engine.py` — 5 tests: denied/escalated/approved explanation shapes, factor ordering, API endpoint round-trip.
- [x] **Test result: 434 passed, 10 xfailed, 0 failed** ✅ (was 429 before this phase)

---

### Phase 17 — Slack Notifications

DENIED and ESCALATED governance verdicts now trigger an instant Slack Block Kit message
to a Slack channel via Incoming Webhook — no one needs to watch the dashboard 24/7.

**`src/notifications/slack_notifier.py`** (NEW)
- [x] `send_verdict_notification(verdict, proposed_action)` — async, fire-and-forget.
- [x] Builds a Slack Block Kit message: verdict badge (🚫/⚠️), resource/agent/action facts, SRI composite + 4-dimension breakdown, reasoning (≤300 chars), top policy violation, "View in Dashboard" button, timestamp.
- [x] APPROVED verdicts silently skipped. Empty `SLACK_WEBHOOK_URL` silently skipped.
- [x] Retries once after 2 s on network failure. Never raises — all exceptions logged.

**`src/core/pipeline.py`**
- [x] `asyncio.create_task(send_verdict_notification(verdict, action))` after every verdict (fire-and-forget). Wrapped in `try/except` so notification errors never affect governance outcomes.

**`src/config.py`**
- [x] `slack_webhook_url: str = ""` — empty = disabled (zero-config default).
- [x] `slack_notifications_enabled: bool = True` — master on/off switch.
- [x] `dashboard_url: str = "http://localhost:5173"` — configurable "View in Dashboard" link in the message.

**`src/api/dashboard_api.py`** — 2 new endpoints (17 total)
- [x] `GET /api/notification-status` — returns `{slack_configured, slack_enabled}` for the dashboard indicator.
- [x] `POST /api/test-notification` — sends a sample DENIED Slack Block Kit message; useful for verifying the integration without running a full scan.

**Frontend**
- [x] `dashboard/src/App.jsx` — 🔔 Slack pill in header: green clickable button when webhook configured (click sends test notification with live "Sending… → ✓ Sent!" feedback), grey static pill when not configured.
- [x] `dashboard/src/api.js` — `fetchNotificationStatus()`, `testSlackNotification()` helpers.

**`.env.example`**
- [x] New `SLACK_WEBHOOK_URL`, `SLACK_NOTIFICATIONS_ENABLED`, `DASHBOARD_URL` entries.

**Tests**
- [x] `tests/test_slack_notifier.py` — 5 tests: denied sends, escalated sends, approved skips, no-webhook skips, notification failure does not block pipeline.
- [x] **Test result: 429 passed, 10 xfailed, 0 failed** ✅ (was 424 before this phase)

### Phase 16 — Scan Durability, Live Log & Agent Action Menus

Five dashboard issues fixed; scan results now survive browser refresh and server restart.

**`src/core/scan_run_tracker.py`** (NEW)
- [x] Durable scan-run store, mirrors `DecisionTracker` pattern: Cosmos DB live / local JSON mock.
- [x] `upsert(record)`, `get(scan_id)`, `get_latest_completed_by_agent_type(agent_type)`, `record_event(scan_id, ts)`
- [x] Auto-creates `governance-scan-runs` container in live mode (`PartitionKey("/agent_type")`).
- [x] Graceful fallback to `data/scans/*.json` if Cosmos init fails.

**`src/config.py`**
- [x] `cosmos_container_scan_runs: str = "governance-scan-runs"` added.

**`src/api/dashboard_api.py`** — 3 new endpoints + durable persistence
- [x] `_get_scan_tracker()` / `_persist_scan_record()` / `_get_scan_record()` helpers — memory-first, durable fallback.
- [x] `_make_scan_record()` now also accepts `resource_group`; initialises `event_count`, `last_event_at`, `totals`.
- [x] `_run_agent_scan()` emits 8 event types: `scan_started`, `discovery`, `analysis`, `reasoning`, `proposal`, `evaluation`, `verdict`, `persisted`, `scan_complete` / `scan_error`.
- [x] Cancellation writes persisted `cancelled` status; empty-proposal runs persist as `complete` with 0 verdicts.
- [x] `GET /api/scan/{scan_id}/stream` — SSE endpoint; `asyncio.Queue` per scan; handles late connections with synthetic terminal event.
- [x] `PATCH /api/scan/{scan_id}/cancel` — validates via `_get_scan_record()`; returns 400 if not running.
- [x] `GET /api/agents/{name}/last-run` — durable scan tracker first, audit trail fallback; returns `proposals_count`, `evaluations_count`, `totals`, timestamps.
- [x] `GET /api/scan/{scan_id}/status` and `stream` now both use `_get_scan_record()` so they survive server restarts.

**Frontend**
- [x] `dashboard/src/components/LiveLogPanel.jsx` — styles added for `analysis`, `reasoning`, `proposal`, `evaluation` event types; backward compat for old `agent_returned` / `evaluating` names.
- [x] `dashboard/src/components/ConnectedAgents.jsx` — `hasScanId` bug fixed (uses real `scanId`, not boolean); "View Live Log" falls back to `fetchAgentLastRun` to recover scan_id; `LastRunPanel` shows timestamp + counts + reasoning chain.
- [x] `dashboard/src/api.js` — `fetchAgentLastRun` JSDoc updated with enriched payload fields.

**Tests**
- [x] `tests/test_dashboard_api.py` — fixture wires isolated `ScanRunTracker`; clears `_scans`, `_scan_events`, `_scan_cancelled` per test.
- [x] `TestScanDurabilityAndStreaming` (4 new tests): status fallback from durable store, last-run counts/timestamps, SSE detailed event sequence, cancellation persistence.
- [x] **Test result: 424 passed, 10 xfailed, 0 failed** ✅ (was 420 before this phase)

### Phase 15 — Validator Contract Fixes

Second-pass audit against the Phase 12/13 prompt contract.  8 findings confirmed and fixed.

**Ops agents — mock gate removed from `scan()`**
- [x] `_use_framework` now gates only on `bool(azure_openai_endpoint)`. `USE_LOCAL_MOCKS` no longer
  bypasses the framework — if the endpoint is set the live path always runs (with mock azure_tools
  when `USE_LOCAL_MOCKS=true`). If no endpoint is configured, `scan()` returns `[]` + log.
- [x] `_scan_rules()` remains as a private method for direct test access (CI rule-engine tests).
- [x] `tests/test_cost_agent.py` + `tests/test_monitoring_agent.py` — fixtures and all
  custom-resource-path tests updated to call `agent._scan_rules()` directly instead of
  `await agent.scan()` (which now correctly returns `[]` without an endpoint configured).

**All 5 generic tools registered in every ops agent**
- [x] `CostOptimizationAgent` gained `query_activity_log` (check recent creates before flagging
  waste) and `list_nsg_rules` (assess security posture alongside cost).
- [x] `MonitoringAgent` gained `query_activity_log` (correlate recent changes with incidents) and
  `list_nsg_rules` (diagnose network-layer reliability issues).
- [x] `DeployAgent` already had all 5 — unchanged.
- [x] All three agents now pass the full 5-tool array to `client.as_agent(tools=[...])`.

**Deploy agent — generic lifecycle tag logic**
- [x] `_LIFECYCLE_TAGS = {"backup","disaster-recovery","purpose"}` removed.  Replaced with
  `_CLASSIFICATION_TAGS = frozenset({"environment","criticality"})`.  The rule-based
  `_detect_missing_lifecycle_tags()` now flags resources that have ONLY classification tags
  — any additional tag is treated as lifecycle metadata, regardless of key name.

**Cost agent runtime prompt narrowing fixed**
- [x] `_AGENT_INSTRUCTIONS` already instructs GPT to query all resource types.  The `agent.run()`
  prompt previously overrode this with "discover VMs and clusters".  Shortened to a single
  scoped sentence — instructions drive the agent fully.

**API scan endpoints — config default resource group**
- [x] `Settings` gained `default_resource_group: str = ""` (env var `DEFAULT_RESOURCE_GROUP`).
- [x] All 4 scan endpoints (`/api/scan/cost|monitoring|deploy|all`) now compute
  `rg = body.resource_group or settings.default_resource_group or None` before passing to
  `_run_agent_scan` — body overrides config; config overrides whole-subscription scan.

**Demo — A2A server auto-starts**
- [x] `demo_live.py` now imports `app` from `src.a2a.ruriskry_a2a_server` and starts it as a
  background `asyncio.Task` on port 8001 before running scenarios.  Cleanly stopped with
  `server.should_exit = True` + task cancellation after all scenarios complete.
- [x] `scenario_1_alert_driven_scaleup` signature changed to `str | None = None`; hardcoded
  `resource_group or "ruriskry-prod-rg"` fallback removed — `None` = whole subscription.

**`query_metrics` — `"current"` field added**
- [x] Live path: `"current": round(values[-1], 2)` — most-recent data point per metric.
- [x] Mock path: `"current": <avg_value>` added to every metric dict in `_mock_query_metrics`.

**Test result: 420 passed, 10 xfailed, 0 failed** ✅ (no regression from Phase 14)

### Phase 14 — Verification & Fixes (commit ee2c0fd)
Comprehensive correctness audit of Phase 12 and Phase 13.  All findings fixed.

**`src/infrastructure/azure_tools.py`**
- [x] Live-mode exceptions now raise `RuntimeError` (descriptive message + "run az login"
  hint) instead of silently falling back to mock data.  Previously all 3 tools
  (`query_resource_graph`, `query_metrics`, `query_activity_log`) swallowed the error and
  returned seed-data responses — hiding connectivity problems.
- [x] `get_resource_details` and `list_nsg_rules`: seed-data fallback code moved inside
  `if _use_mocks():` guard — cannot execute in live mode even on exception.

**Operational agents — throttling**
- [x] `cost_agent.py`, `monitoring_agent.py`, `deploy_agent.py`: `agent.run(prompt)` now
  called via `run_with_throttle()` — same asyncio.Semaphore + exponential back-off
  protection that governance agents already had.

**`demo_live.py`**
- [x] Hardcoded `"ruriskry-prod-rg"` replaced with argparse `--resource-group / -g` CLI
  argument (`None` default → scans whole subscription).  All 3 scenarios parameterised.

**`tests/test_agent_agnostic.py`** (NEW — 22 tests)
- [x] `TestCostAgentAgnostic` (5) — KQL prompt covers all resource types, no seed import,
  accepts any RG, no hardcoded names, returns `[]` on Azure failure
- [x] `TestDeployAgentAgnostic` (5) — generic tag language, no hardcoded tag keys,
  accepts any RG, no seed import, returns `[]` on failure
- [x] `TestMonitoringAgentAgnostic` (4) — accepts arbitrary alert payload, any RG, `[]` on failure
- [x] `TestAzureTools` (3) — RuntimeError on connection failure; KQL passes to SDK
  unchanged (sys.modules injection); mock metrics returns structured dict
- [x] `TestScanAPIEndpoints` (5) — scan_id returned, 3 scan_ids for /all, status endpoint,
  custom RG passthrough, alert-trigger webhook
- [x] **Test result: 420 passed, 10 xfailed, 0 failed** ✅ (was 398 before this phase)

### Phase 13 — Agent Scan Triggers + Dashboard Controls
- [x] `src/api/dashboard_api.py` — 5 new endpoints using FastAPI `BackgroundTasks`:
  - `POST /api/scan/cost` — triggers `CostOptimizationAgent` in background, returns `{scan_id}`
  - `POST /api/scan/monitoring` — triggers `MonitoringAgent` in background
  - `POST /api/scan/deploy` — triggers `DeployAgent` in background
  - `POST /api/scan/all` — triggers all 3 agents simultaneously, returns `{scan_ids: [...]}`
  - `GET /api/scan/{scan_id}/status` — poll `running|complete|error` + verdicts
  - `ScanRequest(BaseModel)` — optional `resource_group: str | None` body
  - `_scans: dict[str, dict]` — in-memory scan store (keyed by UUID)
  - `_run_agent_scan(scan_id, agent_type, resource_group)` — background coroutine: runs
    agent, evaluates every proposal through full pipeline, records to audit trail
- [x] `dashboard/src/api.js` — added `triggerScan()`, `triggerAllScans()`, `fetchScanStatus()`
- [x] `dashboard/src/components/AgentControls.jsx` — **NEW**: Agent Controls panel
  - Resource group text input (optional — empty = scan whole subscription)
  - 3 per-agent buttons (Cost / SRE / Deploy) with spinner + status text
  - "Run All Agents" button triggers all 3 simultaneously
  - Polls `GET /api/scan/{id}/status` every 2s via `setInterval`; calls `onScanComplete()`
    when done so the dashboard auto-refreshes evaluations
  - `useRef` for interval IDs (avoids unnecessary re-renders on poll ticks)
- [x] `dashboard/src/App.jsx` — imports `AgentControls`, renders after `ConnectedAgents`,
  passes `fetchAll` as `onScanComplete` callback
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅
- [x] Commit: `94e0b17` — `feat(api): agent trigger endpoints and dashboard scan controls`
- [x] Learning: `learning/24-agent-triggers.md`

### Environment-Agnosticism Fixes (commit 66cc5ee)
- [x] `src/operational_agents/cost_agent.py` — `_AGENT_INSTRUCTIONS` broadened:
  - KQL no longer specifies only VM + AKS; now instructs GPT to discover all cost-heavy
    resources (App Service, SQL, Cosmos, etc.) with an example query it can adapt
  - Metric guidance generalised per resource type (not CPU-only)
- [x] `src/operational_agents/deploy_agent.py` — `_AGENT_INSTRUCTIONS` generalised:
  - Step 4 no longer names specific tag keys (`backup`, `disaster-recovery`, `purpose`)
  - GPT now flags resources with zero lifecycle/ownership tags of any kind, regardless of
    exact key names — eliminates false positives in orgs with different tag schemas
  - `_LIFECYCLE_TAGS` set documented as org-specific example (used only in mock/CI path)
- [x] All 3 ops agents — live-mode fallback behaviour changed:
  - Exception handler: `return self._scan_rules()` → `return []` (seed-data proposals in
    live mode would be false positives for any real environment)
  - Empty proposals from GPT: `proposals_holder if proposals_holder else _scan_rules()`
    → `return proposals_holder` (no issues found is a valid outcome, not a fallback trigger)
  - `_scan_rules()` is now only called when `USE_LOCAL_MOCKS=true` (mock/CI path)
- [x] `src/infrastructure/azure_tools.py`:
  - `_mock_query_metrics` default CPU changed from 20% (at right-sizing threshold) to 50%
    (clearly busy) — unknown resources no longer trigger false-positive cost proposals
  - `_mock_query_resource_graph` now extracts `resourceGroup =~` filter from KQL and
    applies it to seed data; added 6 new resource type patterns (App Service, SQL, Cosmos,
    ACR, Azure Firewall, Log Analytics)
  - `_mock_activity_log` now uses `resource_group` parameter to derive resource names
    rather than hardcoding `vm-web-01`/`nsg-east-prod`/`ruriskryprodprod` — works for
    any resource group name
- [x] `dashboard/src/components/AgentControls.jsx` — default RG changed from
  `'ruriskry-prod-rg'` to `''` (empty → API sends `null` → agents scan whole subscription)
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅
- [x] Commit: `66cc5ee` — `fix(agents): address environment-agnosticism review findings`
- [x] Learning: `learning/25-environment-agnosticism.md`

### Phase 12 — Intelligent Ops Agents
- [x] `src/infrastructure/azure_tools.py` — **NEW**: 5 generic sync Azure investigation tools:
  - `query_resource_graph(kusto_query)` — KQL query via `ResourceGraphClient`; discovers resources
  - `query_metrics(resource_id, metric_names, timespan)` — real CPU/memory data via `MetricsQueryClient`
  - `get_resource_details(resource_id)` — full resource info via Resource Graph
  - `query_activity_log(resource_group, timespan)` — recent changes via `LogsQueryClient` (LA workspace)
  - `list_nsg_rules(nsg_resource_id)` — actual NSG security rules via Resource Graph
  - Each: `DefaultAzureCredential` live mode + realistic mock fallback from `seed_resources.json`
  - All sync (work directly inside `@af.tool` without `asyncio.run()` conflicts)
- [x] `src/operational_agents/cost_agent.py` — **rewritten**: Senior FinOps Engineer persona
  - Tools: `query_resource_graph`, `query_metrics`, `get_resource_details`, `propose_action`
  - GPT-4.1 discovers VMs, checks 7-day avg CPU, only proposes when evidence shows waste (< 20%)
  - `propose_action` tool validates ActionType/Urgency enums, parses ARM resource IDs
  - `_scan_rules()` for mock/CI fallback (called only when `USE_LOCAL_MOCKS=true`; live-mode
    exception now returns `[]` — see environment-agnosticism fixes above)
- [x] `src/operational_agents/monitoring_agent.py` — **rewritten**: Senior SRE persona
  - New `alert_payload` parameter: alert-driven mode receives Azure Monitor webhook data
  - Alert mode: confirms metric with real data before proposing remediation
  - Scan mode: proactive reliability scan across a resource group
  - Tools: `query_metrics`, `get_resource_details`, `query_resource_graph`, `propose_action`
- [x] `src/operational_agents/deploy_agent.py` — **rewritten**: Senior Platform Engineer persona
  - GPT-4.1 discovers NSGs, inspects actual rules via `list_nsg_rules`, checks activity logs
  - Tools: `query_resource_graph`, `list_nsg_rules`, `get_resource_details`, `query_activity_log`, `propose_action`
- [x] `src/api/dashboard_api.py` — `POST /api/alert-trigger` endpoint added
  - Receives Azure Monitor alert webhook body (resource_id, metric, value, threshold)
  - Calls `MonitoringAgent.scan(alert_payload=...)` → `pipeline.evaluate()` for each proposal
  - Returns proposals + governance verdicts in one response
  - CORS updated to allow POST methods
- [x] `demo_live.py` — **NEW**: Phase 12 two-layer intelligence demo
  - Scenario 1: CPU alert → MonitoringAgent investigates vm-web-01 → SCALE_UP proposal
  - Scenario 2: FinOps scan → CostAgent discovers idle vm-dr-01 → SCALE_DOWN proposal
  - Scenario 3: Security review → DeployAgent audits nsg-east-prod → MODIFY_NSG proposal
  - Each: shows GPT-4.1 reasoning (Layer 1) + RuriSkry SRI verdict (Layer 2)
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅
- [x] Commit: `af1bf28` — `feat(agents): environment-agnostic intelligent ops agents`
- [x] Learning: `learning/23-intelligent-agents.md`

### Phase 11 — Mini Production Environment
- [x] `infrastructure/terraform-prod/main.tf` — 14 Azure resources in `ruriskry-prod-rg`:
  - `vm-dr-01` (Standard_B1ms, Ubuntu) — idle DR VM; cost agent → `DELETE` → **DENIED**
    (tags: `disaster-recovery=true`, `environment=production`, `owner=platform-team`, `cost-center=infrastructure`)
  - `vm-web-01` (Standard_B1ms, Ubuntu) — active web server; SRE agent → `SCALE_UP` → **APPROVED**
    (tags: `tier=web`, `environment=production`, `owner=web-team`, `cost-center=frontend`)
  - `payment-api-prod-{suffix}` (App Service Basic B1) — payment microservice; `critical=true`
    dependency of vm-web-01 that raises blast radius for any web-tier action
  - `nsg-east-prod` (NSG, HTTP/HTTPS allow) — deploy agent → open port 8080 → **ESCALATED**
    (affects all workloads behind subnet gateway; tags: `managed-by=platform-team`)
  - `ruriskryprod{suffix}` (Storage LRS) — shared dependency for all three resources
  - Auto-shutdown at 22:00 UTC on both VMs (saves ~$1/day between demo runs)
  - CPU metric alert on `vm-web-01` (>80%, 15-min window) — triggers monitoring agent
  - Heartbeat scheduled-query alert on `vm-dr-01` (no heartbeat in 15 min) — triggers cost agent
  - Log Analytics workspace + Monitor action group backing both alerts
- [x] `infrastructure/terraform-prod/variables.tf` — 6 variables: `subscription_id`, `location`,
  `suffix` (regex-validated, drives globally-unique names), `vm_admin_username`,
  `vm_admin_password` (sensitive, 12-char min), `alert_email`
- [x] `infrastructure/terraform-prod/outputs.tf` — all resource IDs, names, tags, IPs,
  App Service URL, `seed_resources_ids` helper output for updating `data/seed_resources.json`
- [x] `infrastructure/terraform-prod/terraform.tfvars.example` — template with all placeholders
- [x] `infrastructure/terraform-prod/README.md` — governance scenario SRI score breakdowns,
  deploy/destroy commands, cost table (~$0.35/day with auto-shutdown), agent install note
- [x] `data/seed_resources.json` — new `ruriskry-prod-rg` resources added with real Azure ID paths
  (placeholder subscription ID until `terraform apply`). Legacy mock resources (`vm-23`,
  `api-server-03`, `nsg-east`, etc.) **kept** for test compatibility.
- [x] `.gitignore` — `infrastructure/terraform-prod/` tfstate and tfvars entries added
- [x] `learning/21-mini-prod-environment.md` — IaC concepts, tagging strategy, auto-shutdown
  cost math, full governance scenario walkthrough for a non-programmer audience (gitignored)
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅ (seed_resources still has all legacy names)

#### Phase 11 Bugfix — Azure capacity/quota constraints + region switch
- [x] `infrastructure/terraform-prod/main.tf` — VM size `Standard_B1s` → `Standard_B2ls_v2`
  (B1s/B1ms capacity unavailable in eastus/eastus2 on trial subscriptions; B2ls_v2 available in canadacentral)
- [x] `infrastructure/terraform-prod/main.tf` — App Service plan `B1` → `F1`
  (F1 free tier sufficient for governance demo; saves ~$0.43/day)
- [x] `infrastructure/terraform-prod/variables.tf` — default `location` changed to `canadacentral`
  (eastus/eastus2 had consistent quota failures; canadacentral has reliable B2ls_v2 + F1 availability)
- [x] `infrastructure/terraform-prod/variables.tf` — location description updated (removed eastus2 reference)
- [x] `infrastructure/terraform-prod/terraform.tfvars.example` — location updated to `canadacentral`, `vm_size` added explicitly
- Demo intent unchanged: governance verdicts (DENIED/APPROVED/ESCALATED) are tag-driven,
  not SKU-driven — swapping VM size has zero effect on SRI scoring

#### Phase 11 Enhancement — CPU stress automation + AMA/DCR + Bastion removal
- [x] `infrastructure/terraform-prod/main.tf` — `custom_data` (cloud-init) added to `vm-web-01`:
  installs `stress-ng` + adds cron job (`*/30 * * * *`, 20-min CPU spike) on first boot.
  CPU alert fires naturally every 30 min without manual intervention or SSH access.
  Cron persists across deallocation (OS disk preserved); only lost on `terraform destroy`.
- [x] `infrastructure/terraform-prod/main.tf` — Azure Monitor Agent (AMA) VM extension added
  to both VMs (`azurerm_virtual_machine_extension`); Data Collection Rule (DCR) +
  associations added — heartbeat alert now uses real telemetry, not "no data" state
- [x] `infrastructure/terraform-prod/main.tf` — Azure Bastion removed (subnet, public IP, host,
  SSH NSG rule). SSH not needed — VMs are governance targets, not interactive boxes.
  Saves ~$4.56/day. Use `az vm run-command invoke` for any one-off commands.
- [x] `infrastructure/terraform-prod/main.tf` — dynamic cost lookup map (`vm_hourly_rate_usd_by_sku`)
  added to `locals`; `outputs.tf` now prints actual hourly rate for the configured SKU
- [x] `infrastructure/terraform-prod/README.md` — updated: SKU, region, cost table,
  cloud-init note, AMA/DCR note, Bastion removal note

#### Phase 11 Bugfix — Storage ip_rules `/32` rejection (commit 31b40ba)
- [x] `infrastructure/terraform-prod/main.tf` — split `locals` into two:
  - `local.allowed_source_cidr` → `<ip>/32` — used for NSG `source_address_prefix` (NSG accepts `/32`)
  - `local.storage_allowed_ip` → plain IP — used for storage `ip_rules` (Azure Storage rejects `/31` and `/32` CIDRs)
  - `local.raw_public_ip` — intermediate: `trimspace(api.ipify.org response)`, consumed by both
  - For override CIDRs ending in `/32`: `cidrhost()` strips to plain IP for storage; NSG keeps the `/32`
- [x] `infrastructure/terraform-prod/outputs.tf` — added `storage_allowed_ip` output alongside
  the existing `nsg_allowed_source_cidr` so both effective values are visible after apply

### Phase 10 — A2A Protocol
- [x] `src/a2a/ruriskry_a2a_server.py` — `RuriSkryAgentExecutor(AgentExecutor)` routes
  tasks through the governance pipeline; streams progress via `updater.new_agent_message()` +
  `await updater.update_status(TaskState.working, msg)`; returns `GovernanceVerdict` as A2A
  artifact. Agent Card at `/.well-known/agent-card.json`
  with 3 skills: `evaluate_action`, `query_decision_history`, `get_resource_risk_profile`.
- [x] `src/a2a/operational_a2a_clients.py` — `CostAgentA2AClient`, `MonitoringAgentA2AClient`,
  `DeployAgentA2AClient` — each wraps the corresponding operational agent, uses
  `A2ACardResolver` for discovery, `A2AClient.send_message_streaming()` for SSE transport,
  `httpx.AsyncClient` for async HTTP.
- [x] `src/a2a/agent_registry.py` — `AgentRegistry` persists agent stats to
  `data/agents/` (mock) or Cosmos DB container `governance-agents` (live).
  Methods: `register_agent()`, `get_connected_agents()`, `get_agent_stats()`, `update_agent_stats()`.
- [x] `src/api/dashboard_api.py` — added `GET /api/agents` and `GET /api/agents/{name}/history`.
- [x] `demo_a2a.py` — A2A end-to-end demo: server in background thread, 3 scenarios
  (DENIED / APPROVED / ESCALATED), agent registry summary.
- [x] `requirements.txt` — pinned `agent-framework-a2a==1.0.0b260225`, `a2a-sdk==0.3.24`,
  `httpx==0.28.1`.
- [x] `tests/test_a2a.py` — 20 tests: Agent Card, registry CRUD, executor (mock pipeline),
  dashboard API endpoints.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅

### Phase 10 Bug Fixes (commit 1fee7d1)
- [x] `src/a2a/ruriskry_a2a_server.py` — `DecisionTracker().record(verdict)` added after
  `pipeline.evaluate()`; A2A verdicts now written to Cosmos DB audit trail (were silently dropped).
- [x] `infrastructure/terraform-core/main.tf` — `azurerm_cosmosdb_sql_container "governance_agents"`
  added with `partition_key_paths = ["/name"]`; container now exists in Terraform.
- [x] `src/a2a/operational_a2a_clients.py` — `agent_card_url=self._server_url` in all 3 clients
  (was `""` — empty string stored in registry).
- [x] `src/api/dashboard_api.py` — `get_recent(limit=1000)` raised from 200; prevents
  silent record truncation before agent-name filtering.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅
- [x] Learning: `learning/20-a2a-bugfixes.md`

### Partition Key Mismatch Fix (commit a09dc96→ earlier)
- [x] `infrastructure/terraform-core/main.tf` — `governance-agents` container partition key
  corrected from `/agent_name` (field that never existed in documents) to `/name`
  (matches the `"name"` field in every registry document and the `partition_key=name`
  value passed by `_load_entry`). Option (b) chosen — zero Python changes required.
- [x] `CONTEXT.md`, `STATUS.md`, `docs/SETUP.md` — docs updated to `/name`
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅

### Runtime Fixes (commits ac6ca2c, 50fac30, 7b62822)
- [x] `src/a2a/operational_a2a_clients.py` — `A2ACardResolver` constructor renamed
  from `http_client=` to `httpx_client=` (a2a-sdk==0.3.24 API). Was causing
  `TypeError` at demo startup — no verdicts reached, `data/agents/` stayed empty.
- [x] `src/api/dashboard_api.py` — replaced two `_get_tracker()._load_all()` calls
  with `_get_tracker().get_recent(limit=10_000)`. `_load_all()` does not exist on
  `DecisionTracker` (it's private to `CosmosDecisionClient`). Was causing HTTP 500
  on `GET /api/metrics` and `GET /api/evaluations/{id}`.
- [x] `tests/test_dashboard_api.py` — removed 17 `@pytest.mark.xfail` decorators from
  `TestGetEvaluation` and `TestGetMetrics`. These tests now pass because the
  `_load_all()` root cause is fixed. Remaining 10 xfails: `TestRecord` tests about
  `tracker._dir` (unrelated Phase 7 issue).
- [x] `dashboard/src/components/ConnectedAgents.jsx` — NEW: agent card grid with
  online/offline status (last_seen < 5 min), mini flex bar chart (approved/escalated/denied).
- [x] `dashboard/src/components/LiveActivityFeed.jsx` — NEW: real-time feed of
  recent evaluations, relative time display, VerdictBadge.
- [x] `dashboard/src/App.jsx` — `fetchAll()` extracted for silent background refresh;
  `setInterval(5000)` auto-refresh with `clearInterval` cleanup; SRI gauge shows
  triggering `agent_id`.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅
- [x] Learning: `learning/19-dashboard-a2a.md`

### TaskUpdater Async Await Fix (commit 5094313)
- [x] `src/a2a/ruriskry_a2a_server.py` — Added `await` to `submit()`,
  `start_work()`, `add_artifact()`, and `complete()` in `RuriSkryAgentExecutor.execute()`.
  These are all `async def` in the a2a-sdk. Calling
  them without `await` creates coroutine objects that Python silently discards
  (no error raised). The artifact was never enqueued → client stream received no
  `TaskArtifactUpdateEvent` → `verdict_json` stayed `None` → `send_action_to_skry`
  returned `None` → `_update_registry()` never called → dashboard showed
  "No A2A agents connected yet" even after all previous fixes.
- [x] `tests/test_a2a.py` — Updated 3 `TestRuriSkryAgentExecutor` tests from
  `updater_instance = MagicMock()` to `AsyncMock()`. `MagicMock` objects cannot be
  `await`ed; `AsyncMock` supports both sync calls and `await` automatically.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅ (20/20 A2A tests pass)

### A2A SDK new_agent_message Sync API Fix (commit 2e061cb)
- [x] `src/a2a/ruriskry_a2a_server.py` — `new_agent_message()` in a2a-sdk 0.3.24 is
  **synchronous** — it only creates a `Message` object and does not enqueue it. Previous
  code called `await updater.new_agent_message(...)` which raised
  `TypeError: 'Message' object can't be awaited` at runtime. Fixed to two-step pattern:
  `msg = updater.new_agent_message([Part(...)])` (sync) then
  `await updater.update_status(TaskState.working, message=msg)` (async enqueue).
  Extracted `_progress()` inner async helper to avoid repeating the pattern 5 times.
  Added `TaskState` to imports from `a2a.types`.
- [x] `demo_a2a.py` — replaced box-drawing Unicode characters (`━` `═`) with plain ASCII
  (`-` `=`) to fix `UnicodeEncodeError` on Windows cp1252 consoles.
- [x] **Test result: 505 passed, 0 failed** ✅

### AgentRegistry Cosmos Key + Demo Mock Fix (commit 3534d0e)
- [x] `src/a2a/agent_registry.py` — `cosmos_key` now resolved before the `_is_mock`
  check, adding `or not self._cosmos_key` to the condition (mirrors `CosmosDecisionClient`
  exactly). Previously the key was resolved inside the live-mode `try` block but not
  guarded — registry entered live mode with an empty key, `_save_entry()` called
  `container.upsert_item()`, Cosmos rejected with auth error, exception propagated with
  no catch → all agent writes silently dropped → dashboard showed "No A2A agents connected".
  `CosmosDecisionClient` always had this guard and fell to mock correctly; now both clients
  behave identically.
- [x] `demo_a2a.py` — removed `os.environ.setdefault("USE_LOCAL_MOCKS", "true")`.
  `setdefault` only writes if the key is absent; because Python loads imports before
  dotenv files, the setdefault always fired first and forced mock mode regardless of what
  `.env` said. Demo now reads `USE_LOCAL_MOCKS` from `.env` like the dashboard API does.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅

### SSE Event Unwrapping Fix (commit 72d5204)
- [x] `src/a2a/operational_a2a_clients.py` — `A2AClient.send_message_streaming()`
  yields `SendStreamingMessageResponse` objects, not raw events. The actual
  `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent` is at `.root.result`.
  Previous code checked `isinstance(event.root, TaskArtifactUpdateEvent)` which
  was always False → `verdict_json` never set → `send_action_to_skry` always
  returned `None` → `data/agents/` stayed empty → dashboard showed
  "No A2A agents connected yet". Fix: added `result = getattr(root, "result", root)`
  and switched isinstance checks to use `result`.
- [x] **Test result: 398 passed, 10 xfailed, 0 failed** ✅

### Phase 9 — Async-First Refactor
- [x] **Issue 1 — async-first**: all 7 agent `evaluate()`/`scan()` methods → `async def`;
  `asyncio.run()` removed everywhere; callers use `await`
- [x] `src/core/pipeline.py` — `ThreadPoolExecutor` replaced with `asyncio.gather()`
  (4 governance agents + 3 operational agents run concurrently in the same event loop)
- [x] `src/core/interception.py` — `intercept()` and `intercept_from_dict()` → `async def`
- [x] `src/mcp_server/server.py` — `skry_evaluate_action()` → `async def`
- [x] `demo.py` — `scenario_1/2/3()` and `main()` → `async def`, entry: `asyncio.run(main())`
- [x] `src/api/dashboard_api.py` — all 4 endpoints → `async def`
- [x] **Issue 2 — credentials**: `AzureCliCredential` → `DefaultAzureCredential` in all 7 agents
  (works for `az login` locally and Managed Identity in Azure)
- [x] **Issue 3 — pin dep**: `requirements.txt`: `agent-framework-core>=1.0.0rc2` → `==1.0.0rc2`
- [x] **Issue 4 — xfail**: 27 pre-existing failures marked `@pytest.mark.xfail`
  (10 × `TestRecord` — `tracker._dir` gone; 17 × dashboard — `_load_all()` gone; both Phase 7)
- [x] Installed `pytest-asyncio==1.3.0` (was missing from environment)
- [x] **Test result: 361 passed, 27 xfailed, 0 failed** ✅
- [x] Commit: `164b713` — `fix(async): refactor to async-first, pin deps, mark known xfails`
- [x] Learning: `learning/17-async-refactor.md`

---

## Current Configuration

```
USE_LOCAL_MOCKS=false                   ← live Azure is the default
AZURE_OPENAI_ENDPOINT=https://ruriskry-foundry-psc0des.cognitiveservices.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-41
AZURE_SEARCH_ENDPOINT=https://ruriskry-search-psc0des.search.windows.net
AZURE_SEARCH_INDEX=incident-history     ← seeded with 7 incidents
COSMOS_ENDPOINT=https://ruriskry-cosmos-psc0des.documents.azure.com:443/
COSMOS_DATABASE=ruriskry
COSMOS_CONTAINER_DECISIONS=governance-decisions
AZURE_KEYVAULT_URL=https://ruriskry-kv-psc0des.vault.azure.net/
A2A_SERVER_URL=http://localhost:8000    ← A2A server base URL (Phase 10)
```

API keys are **not** in `.env` — fetched at runtime from Key Vault.
Run `az login` locally before starting any live-mode service.

---

## How to Run

```bash
# (Optional) Seed demo incidents into AI Search — dev/demo only
# python scripts/seed_data.py

# End-to-end governance demo — direct Python pipeline (3 scenarios)
python demo.py

# Phase 12 — two-layer intelligence demo (ops agents investigate + RuriSkry evaluates)
python demo_live.py

# A2A protocol demo — server + 3 agent clients via A2A (Phase 10)
python demo_a2a.py

# RuriSkry as A2A server (Phase 10)
uvicorn src.a2a.ruriskry_a2a_server:app --host 0.0.0.0 --port 8000

# FastAPI dashboard (includes /api/agents + alert-trigger endpoints)
uvicorn src.api.dashboard_api:app --reload

# Trigger monitoring agent via API (simulates Azure Monitor alert webhook):
# POST /api/alert-trigger  body: {"resource_id":"vm-web-01","metric":"Percentage CPU","value":95}

# Unit tests (mock mode — no Azure needed)
pytest tests/ -v
```

---

## Known Issues / Tech Debt

- [ ] `learning/` numbering is inconsistent — files 03, 04, 15 have duplicates from
  mid-sprint renames. Does not affect functionality.
- [ ] Azure AI Search uses BM25 full-text; vector embeddings (semantic ranking) would
  require a separate `text-embedding-3-small` deployment in Foundry.
- [ ] `functions/function_app.py` exists but is not wired into the main pipeline.
  Azure Function deployment is not yet configured.
- [ ] React dashboard (`learning/11-react-dashboard.md`) is documented; frontend lives
  in `dashboard/` (not `ui/`).
- [ ] No CI/CD pipeline — tests run locally only.

---

## Known Limitations (Azure OpenAI Rate Limiting)

**HTTP 429 — Too Many Requests:** All 5 agents hit Azure OpenAI's rate limit during
`demo.py` and fall back to deterministic rules. This means the AI reasoning layer is
not exercised in practice today.

**Why it happens:** Azure OpenAI deployments have a **Tokens Per Minute (TPM)** and
**Requests Per Minute (RPM)** quota. Running 5 governance agents × 3 scenarios = up to
15 concurrent LLM calls exhausts even a generous quota in seconds. The `except Exception`
fallback in each agent catches the 429 and silently continues with rule-based scoring.

**Where to check:** Azure Portal → Azure OpenAI → your deployment → Quotas.
Request a quota increase or add exponential back-off + retry logic in each agent's
`_evaluate_with_framework()` before re-attempting the framework path.

**Impact:** Governance scoring still works correctly (deterministic rules are the
safety floor), but GPT-4.1's semantic reasoning — which should catch things like
equivalent tag formats or novel risk patterns — is never reached.

---

## Phase 21 — Execution Gateway & Human-in-the-Loop (COMPLETE)

> **Implementation guide:** `Adding-Terraform-Feature.md` (step-by-step with code)

**Problem:** APPROVED verdicts are informational only — nothing executes. A direct Azure SDK
execution layer would cause **IaC state drift**: Terraform reverts the change on the next
`terraform apply`.

**Solution:** APPROVED verdicts generate **Terraform PRs** against the IaC repo. A human
reviews and merges; CI/CD runs `terraform apply`; IaC state stays in sync.

**Architecture:**
```
GovernanceVerdict → ExecutionGateway
    ├── DENIED    → blocked (log + Slack alert)
    ├── ESCALATED → awaiting_review (Slack alert + dashboard HITL buttons)
    └── APPROVED  → IaC-managed? → YES → auto-generate Terraform PR
                                 → NO  → manual_required (HITL)
```

**Completed components:**
- [x] `src/core/models.py` — `ExecutionStatus` enum (9 values) + `ExecutionRecord` model
- [x] `src/core/execution_gateway.py` — verdict routing; IaC detection via `managed_by` tag;
  JSON persistence in `data/executions/`; `verdict_snapshot` stored for HITL approve path;
  `KeyError` (unknown ID → 404) vs `ValueError` (wrong state → 400)
- [x] `src/core/terraform_pr_generator.py` — GitHub PR via `PyGithub`; `asyncio.to_thread()` wraps sync API
- [x] `src/config.py` — `iac_github_repo`, `iac_terraform_path`, `execution_gateway_enabled`, `github_token`
- [x] `src/api/dashboard_api.py` — 4 new endpoints (19–22); `_get_resource_tags()` async —
  queries `ResourceGraphClient.get_resource_async()` in live mode; falls back to
  `seed_resources.json` on failure or in mock mode
- [x] `dashboard/src/components/EvaluationDrilldown.jsx` — Section 7: Execution Status panel with Approve/Dismiss buttons
- [x] `dashboard/src/api.js` — `fetchExecutionStatus()`, `approveExecution()`, `dismissExecution()`
- [x] `infrastructure/terraform-prod/main.tf` + `variables.tf` — `iac_repo` + `iac_path` from variables
- [x] `tests/test_execution_gateway.py` (33 tests) — IaC detection, verdict routing, approval, persistence, snapshot
- [x] `tests/test_dashboard_api.py::TestGetResourceTags` (6 tests) — mock path, live path, fallback on failure
- [x] `requirements.txt` — `PyGithub>=2.1.0` (commented as optional)
- [x] `.env.example` — Phase 21 env vars documented

**Env vars (new):**
- `GITHUB_TOKEN` — GitHub PAT with repo write access
- `IAC_GITHUB_REPO` — e.g. `psc0des/ruriskry`
- `IAC_TERRAFORM_PATH` — e.g. `infrastructure/terraform-prod`
- `EXECUTION_GATEWAY_ENABLED` — `false` by default (opt-in)

**Post-deploy fixes (same phase, incremental commits):**
- `dashboard_api._run_agent_scan()`: calls `registry.update_agent_stats()` per verdict + `register_agent()` on 0-proposal scans → Connected Agents panel now shows live `last_seen` and verdict counts after every dashboard-triggered scan
- `LiveLogPanel.jsx`: supports `scanEntries=[{scanId,agentType},…]` prop — opens one SSE stream per agent, merges into one chronological log with coloured agent badges (Cost/SRE/Deploy)
- `AgentControls.jsx`: "Run All Agents" passes all 3 scan IDs → merged live log visible instead of cost-only
- `ExecutionGateway.get_unresolved_proposals()`: returns `(ProposedAction, record)` for all `manual_required` records; `_run_agent_scan()` re-adds them on every scan (deduped) until human clicks Dismiss or agent stops proposing naturally — "flag until fixed"
- `requirements.txt`: `PyGithub>=2.1.0` uncommented (required when `EXECUTION_GATEWAY_ENABLED=true`)

**Test result: 551 passed, 0 failed** (+7 TestUnresolvedProposals; total +46 since Phase 21 start)

---

## Phase 22 — LLM-as-Decision-Maker (COMPLETE)

### Problem
All 4 governance agents used the LLM as a **narrator** — it called a deterministic tool,
the tool computed the score, and the LLM wrote a summary paragraph. The score was 100%
deterministic regardless of the LLM's understanding. This caused the SSH remediation bug:
an ops agent describing "SSH open to 0.0.0.0/0 — fixing" triggered a CRITICAL policy violation
via `reason_pattern` match, auto-DENIED the remediation.

### Solution
Rearchitected all 4 agents to make the LLM a **decision maker**:
1. Deterministic rules run first → produce baseline score
2. LLM receives: action + ops agent reasoning + full policies/context
3. LLM calls new `submit_governance_decision` tool with adjusted score + justification
4. Guardrail clamps LLM adjustment to +/-30 points from baseline
5. Governance engine uses LLM-adjusted score

### Files changed
- `src/core/models.py` — `GovernanceAdjustment` + `LLMGovernanceOutput` models added
- `src/governance_agents/_llm_governance.py` — new: `clamp_score()`, `parse_llm_decision()`, `format_adjustment_text()`
- `src/governance_agents/policy_agent.py` — new `_AGENT_INSTRUCTIONS`, added `submit_governance_decision` tool, enriched prompt with full policy JSON + ops agent reason
- `src/governance_agents/blast_radius_agent.py` — same pattern
- `src/governance_agents/historical_agent.py` — same pattern
- `src/governance_agents/financial_agent.py` — same pattern
- `src/core/governance_engine.py` — critical-violation auto-DENY: `sri_policy >= 40` condition added (respects LLM override)
- `tests/test_llm_governance.py` — 25 new tests: clamp_score, parse_llm_decision, format_adjustment_text, critical-violation softening

**Test result: 621 passed, 0 failed** (+25 new tests for Phase 22)

---

## What's Next (Suggested)

These are ideas, not commitments. Pick up from here:

### Phase 14 — Azure Monitor Alert Webhook Wiring (Priority)

Phase 12 built `POST /api/alert-trigger`; Phase 13 added manual scan triggers from the
dashboard.  Phase 14 closes the production loop by wiring real Azure Monitor alerts:

```
Real flow:
  vm-web-01 CPU > 80% (stress-ng fires every 30 min via cron)
        ↓
  Azure Monitor metric alert fires
        ↓
  Action Group webhook → POST /api/alert-trigger
        ↓
  MonitoringAgent.scan(alert_payload=...) → confirms via query_metrics
        ↓
  GPT-4.1: "7-day avg CPU 82.5%, peak 100% — sustained load. Propose scale_up."
        ↓
  RuriSkry evaluates → APPROVED (SRI < 25, low blast radius)
        ↓
  Verdict written to Cosmos DB, visible on dashboard
```

**Steps:**
1. Expose `POST /api/alert-trigger` publicly (ngrok for demo, or Azure App Service)
2. In Azure Portal: Alerts → Action Groups → add Webhook pointing to the endpoint
3. Test: run `stress-ng` on vm-web-01 → alert fires → end-to-end

### Phase 15 — React Dashboard: Live Agent Intelligence Panel

Add a new dashboard panel showing the two-layer intelligence in real time:
- Layer 1 card: which ops agent fired, what tools it called, the evidence it gathered
- Layer 2 card: RuriSkry's SRI breakdown and verdict

### Phase 16 — Multi-Agent Orchestrator

Build an orchestrator that runs all 3 ops agents on a schedule and pipes proposals
through RuriSkry automatically — fully autonomous cloud governance loop.

- [ ] **Wire Logic App webhook** — Azure Monitor alert → HTTP POST to
  `POST /api/evaluate` or a new `/api/alert-trigger` endpoint
- [ ] **Intelligent monitoring-agent** — queries real Azure Monitor API for metric
  values; GPT-4.1 decides whether to propose scale-up and what SKU based on data
- [ ] **Intelligent cost-agent** — queries Azure Monitor 30-day CPU history for all
  VMs; GPT-4.1 reasons about idle vs standby vs DR before proposing deletion
- [ ] **Semantic policy matching** — policy evaluation should use GPT-4.1 to match
  resource tags semantically (not exact string) — a resource tagged `disaster-recovery:
  true` or `purpose: disaster-recovery` or `dr-role: standby` should all trigger
  `POL-DR-001`; exact string match is brittle against real-world tag drift
- [ ] **Fix 429 rate limiting** — add exponential back-off + retry in
  `_evaluate_with_framework()` across all agents; alternatively request TPM quota
  increase in Azure Portal

### Other Improvements
- [ ] **Vector search** — deploy `text-embedding-3-small` in Foundry, add vector field
  to `incident-history` index, generate embeddings on seed + query
- [ ] **Azure Function deployment** — wire `functions/function_app.py` for serverless
  governance endpoint
- [ ] **CI/CD** — GitHub Actions: run `pytest tests/ -v` on PR, deploy to Azure on merge
- [ ] **More policies** — add `data/policies.json` entries for cost caps, region
  restrictions, tag compliance
- [ ] **More seed incidents** — expand `data/seed_incidents.json` beyond 7 entries
- [ ] **Streaming LLM responses** — stream GPT-4.1 tokens to the dashboard in real time

---

## Secret Names in Key Vault

| Secret Name | Used By |
|-------------|---------|
| `foundry-primary-key` | `AzureOpenAIClient` |
| `search-primary-key` | `AzureSearchClient` |
| `cosmos-primary-key` | `CosmosDecisionClient` |

---

## File Ownership Map

| File | What it does | Last changed |
|------|-------------|-------------|
| `src/core/models.py` | All Pydantic data models — shared contract | Phase 1 |
| `src/core/pipeline.py` | Parallel agent orchestration + `scan_operational_agents()` | Phase 8 |
| `src/core/governance_engine.py` | SRI composite + verdict logic | Phase 2 |
| `src/core/decision_tracker.py` | Verdict → Cosmos DB / JSON | Phase 7 |
| `src/governance_agents/blast_radius_agent.py` | SRI:Infrastructure — Agent Framework + `@tool` | Phase 8 |
| `src/governance_agents/policy_agent.py` | SRI:Policy — Agent Framework + `@tool` | Phase 8 |
| `src/governance_agents/historical_agent.py` | SRI:Historical — Agent Framework + `@tool` | Phase 8 |
| `src/governance_agents/financial_agent.py` | SRI:Cost — Agent Framework + `@tool` | Phase 8 |
| `src/infrastructure/cost_lookup.py` | `get_sku_monthly_cost(sku, location)` — Azure Retail Prices API; module-level cache; None on failure | Phase 19 |
| `src/infrastructure/azure_tools.py` | 5 sync + 5 async (`*_async`) Azure tools; `"current"` field in metrics; RuntimeError on live failure | Phase 15/20 |
| `src/operational_agents/cost_agent.py` | FinOps proposals — 5 tools; `scan()` framework-only; `_scan_rules()` for tests | Phase 15 |
| `src/operational_agents/monitoring_agent.py` | SRE anomaly detection — 5 tools; `scan()` framework-only; `_scan_rules()` for tests | Phase 15 |
| `src/operational_agents/deploy_agent.py` | Security/config proposals — 5 tools; generic lifecycle tag logic | Phase 15 |
| `src/infrastructure/openai_client.py` | GPT-4.1 via Foundry (direct completions) | Phase 7 |
| `src/infrastructure/cosmos_client.py` | Cosmos DB read/write | Phase 6 |
| `src/infrastructure/search_client.py` | Azure AI Search + index seeding | Phase 7 |
| `src/infrastructure/secrets.py` | Key Vault secret resolution | Phase 6 |
| `src/config.py` | All env vars + SRI thresholds + `default_resource_group` + `use_live_topology` | Phase 19 |
| `data/policies.json` | 6 governance policies | Phase 2 |
| `data/seed_incidents.json` | 7 past incidents (also in Azure Search) | Phase 3 |
| `data/seed_resources.json` | Azure resource topology mock | Phase 2 |
| `scripts/seed_data.py` | Index seed_incidents into Azure Search | Phase 5 |
| `src/a2a/ruriskry_a2a_server.py` | A2A server — AgentCard + RuriSkryAgentExecutor + audit trail write; progress via `new_agent_message()` + `update_status(TaskState.working)`; async calls awaited | SDK sync fix |
| `src/a2a/operational_a2a_clients.py` | A2A client wrappers — `httpx_client=`; SSE `.root.result` unwrap | SSE fix |
| `src/a2a/agent_registry.py` | Tracks connected A2A agents + governance stats; cosmos_key guard matches CosmosDecisionClient | Registry fix |
| `src/core/scan_run_tracker.py` | Durable scan-run store — Cosmos / JSON; upsert, get, get_latest_by_agent_type | Phase 16 |
| `src/core/alert_tracker.py` | Durable alert investigation store — Cosmos `governance-alerts` / `data/alerts/*.json` mock | Alerts |
| `src/governance_agents/blast_radius_agent.py` | live-mode branch in `__init__` + `_find_resource()` / `_detect_spofs()` / `_get_affected_zones()` | Phase 19 |
| `src/governance_agents/financial_agent.py` | live-mode branch in `__init__` + `_find_resource()` | Phase 19 |
| `src/infrastructure/resource_graph.py` | `_azure_enrich_topology()` — 5-step KQL topology + cost_lookup; `_azure_get_resource()` + `_azure_list_all()` call it | Phase 19 |
| `tests/test_live_topology.py` | 16 tests — cost_lookup, ResourceGraph enrichment, blast-radius + financial live mode | Phase 19 |
| `tests/test_decision_tracker.py` | 10 xfail markers removed; `tracker._dir` → `tracker._cosmos._decisions_dir` | Phase 19 fix |
| `infrastructure/terraform-prod/main.tf` | `depends-on` + `governs` tags on 4 governed resources | Phase 19 |
| `src/core/explanation_engine.py` | `DecisionExplainer` — factors, counterfactuals, LLM summary, module-level cache | Phase 18 |
| `tests/test_explanation_engine.py` | 5 tests — denied/escalated/approved shapes, factor ordering, API endpoint round-trip | Phase 18 |
| `src/api/dashboard_api.py` | FastAPI REST — 18 endpoints; durable scan store; SSE live log; cancel; last-run; Slack status + test; explanation | Phase 18 |
| `src/notifications/slack_notifier.py` | Block Kit attachments → Slack Incoming Webhook on DENIED/ESCALATED; fire-and-forget | Phase 17 |
| `tests/test_slack_notifier.py` | 5 tests — denied, escalated, approved skip, no-webhook skip, pipeline resilience | Phase 17 |
| `demo_live.py` | Two-layer intelligence demo — A2A server auto-starts; no hardcoded RG fallback | Phase 15 |
| `dashboard/src/components/AgentControls.jsx` | Scan control panel — per-agent buttons, polling, Run All, LiveLogPanel trigger | Phase 16 |
| `dashboard/src/components/LiveLogPanel.jsx` | SSE slide-out panel — 9 event type styles, auto-scroll, EventSource cleanup | Phase 16 |
| `dashboard/src/components/ConnectedAgents.jsx` | Agent card grid — ⋮ action menu, per-agent scan/log/results/history/details panels | Phase 16 |
| `dashboard/src/components/EvaluationDrilldown.jsx` | 6-section full-page drilldown — SRI bars, explanation, counterfactuals, reasoning, JSON audit trail | Phase 18 |
| `dashboard/src/api.js` | Frontend fetch helpers incl. streamScanEvents, cancelScan, fetchAgentLastRun, fetchNotificationStatus, fetchExplanation | Phase 18 |
| `dashboard/src/App.jsx` | Root component — 🔔 Slack pill, drilldown navigation via drilldownEval state | Phase 18 |
| `dashboard/src/components/LiveActivityFeed.jsx` | Real-time feed — rows clickable, onDrilldown prop | Phase 18 |
| `data/scans/` | Local JSON scan-run store (mock mode for ScanRunTracker) | Phase 16 |
| `data/alerts/` | Local JSON alert investigation store (mock mode for AlertTracker) | Alerts |
| `infrastructure/terraform-core/main.tf` | Azure infra — Foundry, Search, Cosmos (2 containers), KV | Phase 10 bugfixes |
| `infrastructure/terraform-prod/main.tf` | Mini prod env — 2 VMs, NSG, storage, App Service, monitor alerts | Phase 11 |
| `infrastructure/terraform-prod/outputs.tf` | Exports all resource IDs, names, tags, URLs | Phase 11 |
| `infrastructure/terraform-prod/variables.tf` | Input variables incl. sensitive vm_admin_password | Phase 11 |
| `data/seed_resources.json` | Azure resource topology — ruriskry-prod-rg resources + legacy mocks | Phase 11 |
| `dashboard/src/App.jsx` | Root component — fetchAll, setInterval, ConnectedAgents, LiveActivityFeed | Runtime fixes |
| `dashboard/src/components/ConnectedAgents.jsx` | Agent card grid with online status + bar chart (NEW) | Runtime fixes |
| `dashboard/src/components/LiveActivityFeed.jsx` | Real-time evaluation feed with relative timestamps (NEW) | Runtime fixes |
| `dashboard/src/api.js` | Frontend fetch helpers incl. fetchAgents() | Runtime fixes |
| `demo_a2a.py` | A2A end-to-end demo (3 scenarios); removed USE_LOCAL_MOCKS setdefault | Registry fix |
