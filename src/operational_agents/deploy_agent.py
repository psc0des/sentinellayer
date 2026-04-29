"""Infrastructure Deploy Agent — proposes deployment and configuration actions.

This is an operational agent (the governed subject). It proposes
infrastructure changes that RuriSkry evaluates before execution.

Phase 12 — Intelligent, evidence-driven agent
----------------------------------------------
The agent now investigates the real Azure environment before proposing changes:

1. Discovers NSGs with ``query_resource_graph``.
2. Inspects actual security rules with ``list_nsg_rules`` — checks for
   missing deny-all inbound rules using real NSG rule data.
3. Reviews recent change activity with ``query_activity_log`` — detects
   failed writes or suspicious recent modifications.
4. Checks resource tags and configuration with ``get_resource_details``.
5. Uses GPT-4.1 to reason about security posture before proposing actions.

The agent is environment-agnostic: accepts an optional
``target_resource_group`` parameter and can scan any Azure environment.

Microsoft Agent Framework tools (live mode)
--------------------------------------------
- ``query_resource_graph(kusto_query)`` — discover NSGs, VMs, storage, databases
- ``list_nsg_rules(nsg_resource_id)`` — inspect actual security rules
- ``get_resource_details(resource_id)`` — check tags, configuration, power state
- ``query_activity_log(resource_group, timespan)`` — review recent changes
- ``get_resource_health(resource_id)`` — Azure Platform availability signal
- ``list_advisor_recommendations(scope, category)`` — Azure Advisor Security tips
- ``list_defender_assessments(scope)`` — Defender for Cloud security assessments
- ``list_policy_violations(scope)`` — Azure Policy non-compliant resources
- ``propose_action(...)`` — submit a validated ProposedAction

In mock mode (USE_LOCAL_MOCKS=true) the deterministic ``_scan_rules()``
fallback runs — reads ``data/seed_resources.json`` and applies heuristics.
"""

import json
import logging
from pathlib import Path

from src.config import settings as _default_settings
from src.core.models import ActionTarget, ActionType, EvidencePayload, ProposedAction, Urgency
from src.operational_agents import is_compliant_reason

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_ID = "deploy-agent"

_DEFAULT_RESOURCES_PATH = (
    Path(__file__).parent.parent.parent / "data" / "seed_resources.json"
)

# Rule-based fallback thresholds (unchanged from Phase 8)
_SPARSE_TOPOLOGY_THRESHOLD: int = 3
_NSG_RESOURCE_TYPE = "Microsoft.Network/networkSecurityGroups"
# Tags used by the rule-based (mock/CI) path to detect lifecycle metadata.
# Classification tags (environment, criticality) are NOT lifecycle tags — they describe
# what a resource is, not how it is managed.  Any tag beyond these indicates that
# lifecycle management (backup, DR, owner, cost-centre, etc.) has been applied.
_CLASSIFICATION_TAGS: frozenset[str] = frozenset({"environment", "criticality"})

# System instructions for the framework agent.
_AGENT_INSTRUCTIONS = """\
You are a Senior Platform/Security Engineer conducting an enterprise
infrastructure security and configuration compliance review.
Microsoft Security APIs have pre-run and detected issues — your job is to confirm,
investigate, and enrich each finding, then look for anything they missed.

━━━ PRIMARY TASK: INVESTIGATE PRE-COMPUTED API FINDINGS ━━━
Findings from Azure Advisor, Defender for Cloud, and Azure Policy are listed
at the top of the prompt. For EACH finding:
1. Call get_resource_details on the resource to confirm the issue is real and current.
2. Call list_nsg_rules if the resource is a network resource (NSG, VNet).
3. Assess blast radius — who else is affected if this resource is compromised?
4. Call propose_action with a complete reason: finding name, confirmed properties,
   blast radius, and specific remediation steps.
Do NOT skip any pre-computed finding — even if you think it is "already known".

━━━ SECONDARY TASK: INDEPENDENT SECURITY DISCOVERY ━━━
After processing all pre-computed findings, scan the resource inventory (above)
or call query_resource_graph to discover issues the APIs missed:

NSG AUDIT — for each NSG, call list_nsg_rules and check:
  CRITICAL (urgency=HIGH) — Internet-exposed management ports:
    sourceAddressPrefix *, Any, or Internet AND port 22/3389/5985/5986/23/21/1433
    → Propose modify_nsg with rule name, port, source prefix, risk description.
  HIGH — Wildcard internet exposure:
    sourceAddressPrefix *, Any, or Internet AND destinationPortRange *
    → All ports exposed to internet. Propose modify_nsg urgency=HIGH.
  MEDIUM — Missing explicit deny-all:
    NSG has inbound Allow rules but no deny-all (access=Deny, port=*, direction=Inbound)
    → Propose modify_nsg urgency=MEDIUM.

STORAGE ACCOUNT SECURITY — for each storage account, call get_resource_details:
  - allowBlobPublicAccess = true → propose update_config (HIGH urgency).
    Reason: "Public blob access enabled — any blob container can be made public."
  - supportsHttpsTrafficOnly = false → propose update_config (HIGH urgency).
    Reason: "HTTP traffic allowed — data transmitted in plaintext."
  - minimumTlsVersion < TLS1_2 → propose update_config (MEDIUM urgency).
  - defaultAction = Allow (no network ACL) → propose update_config (MEDIUM).
  - Storage key not rotated in > 90 days (check lastKeyRotation in activity log or
    resource properties) → propose rotate_storage_key (MEDIUM urgency); set
    config_changes={"key_name": "key1"}. The Execution Agent uses rotate_storage_keys
    automatically. NOTE: requires per-account RBAC — see deployment docs.

DATABASE & KEY VAULT — for each Cosmos DB, SQL server, Key Vault:
  - publicNetworkAccess = Enabled → propose update_config (MEDIUM/HIGH).
  - enableSoftDelete = false on Key Vault → propose update_config (HIGH).
  - enablePurgeProtection = false → propose update_config (MEDIUM).
  - SQL server with no firewall rules (allow all) → propose update_config (HIGH).

VM SECURITY — for each VM, call get_resource_details:
  - OS disk unencrypted → propose update_config (MEDIUM).
  - Password auth on Linux VM → propose update_config (MEDIUM).
  - Public IP with no NSG attached → propose update_config (HIGH).

RECENT CHANGES — call query_activity_log for suspicious failed operations in last 48h.

TAGGING GAPS — resources with ZERO tags → propose update_config (LOW).
Do NOT check for specific tag key names — tag schemas vary by organisation. Flag resources with ZERO tags only.

━━━ RESOURCE DISCOVERY (use this KQL when no inventory is provided) ━━━
Resources
| project id, name, type, location, resourceGroup, tags, sku, properties
| order by type asc

Do NOT add a 'where type in (...)' filter — ALL resource types matter for security.
The Microsoft APIs already know which types have issues — your job is to investigate them.

━━━ URGENCY SCALE ━━━
  HIGH:   Internet-exposed management ports, public blob access, HTTP-only storage,
          KV soft-delete disabled, SQL firewall open to all.
  MEDIUM: Missing deny-all NSG rule, DB on public network, unencrypted disk,
          storage accessible from all networks, purge protection disabled.
  LOW:    Missing tags, configuration hygiene gaps.

IMPORTANT: Propose an action ONLY when a violation is confirmed. Do not propose if
the resource is already compliant. Do not group multiple findings into one proposal —
each security gap needs its own governance verdict.

━━━ YOUR ROLE AND BOUNDARIES ━━━
Your ONLY job is to inspect the live Azure environment and report what you find.
You are a detection tool, not a decision-maker about what is "new" or "already known".

NEVER skip or suppress a finding because you think it was flagged before,
is already being handled, or the user probably knows about it.
You have no memory of previous scans. Every scan is a fresh, independent
inspection of the current state. The governance engine handles deduplication.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DeployAgent:
    """Proposes infrastructure deployment and configuration actions.

    In live mode (USE_LOCAL_MOCKS=false) GPT-4.1 inspects real NSG rules,
    activity logs, and resource configuration before proposing changes.

    In mock mode only the deterministic ``_scan_rules()`` runs.

    Usage::

        agent = DeployAgent()
        proposals: list[ProposedAction] = await agent.scan()

        # Scope to a specific resource group:
        proposals = await agent.scan(target_resource_group="ruriskry-prod-rg")
    """

    def __init__(
        self,
        resources_path: str | Path | None = None,
        cfg=None,
    ) -> None:
        path = Path(resources_path) if resources_path else _DEFAULT_RESOURCES_PATH
        with open(path, encoding="utf-8") as fh:
            data: dict = json.load(fh)

        self._resources: dict[str, dict] = {
            r["name"]: r for r in data.get("resources", [])
        }
        self._all_resources: list[dict] = data.get("resources", [])

        self._cfg = cfg or _default_settings
        self._use_framework: bool = bool(self._cfg.azure_openai_endpoint)
        self.scan_error: str | None = None  # populated if framework call fails

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(
        self,
        target_resource_group: str | None = None,
        inventory: list[dict] | None = None,
    ) -> list[ProposedAction]:
        """Investigate the Azure environment and return configuration proposals.

        Args:
            target_resource_group: Optional resource group to scope the scan.
                When ``None`` the agent scans across the subscription.
            inventory: Optional pre-fetched resource list.  When provided,
                injected into the LLM prompt so the agent can review all
                resources without relying on non-deterministic discovery.

        Returns:
            List of :class:`~src.core.models.ProposedAction` objects.
        """
        if self._cfg.demo_mode:
            logger.info(
                "DeployAgent: DEMO_MODE enabled — returning sample proposals "
                "for pipeline testing (set DEMO_MODE=false for real Azure scanning)."
            )
            return self._demo_proposals()

        if not self._use_framework:
            logger.info(
                "DeployAgent: no Azure OpenAI endpoint configured — "
                "returning no proposals (set AZURE_OPENAI_ENDPOINT to enable live scanning)."
            )
            return []

        self.scan_error = None
        try:
            return await self._scan_with_framework(target_resource_group, inventory)
        except Exception as exc:  # noqa: BLE001
            self.scan_error = str(exc)
            logger.warning(
                "DeployAgent: framework call failed (%s) — returning no proposals "
                "(live-mode fallback to seed data would generate false positives).",
                exc,
            )
            return []

    # ------------------------------------------------------------------
    # Microsoft Agent Framework path (live mode)
    # ------------------------------------------------------------------

    async def _scan_with_framework(
        self, target_resource_group: str | None, inventory: list[dict] | None = None
    ) -> list[ProposedAction]:
        """Run GPT-4.1 with security investigation tools."""
        from openai import AsyncAzureOpenAI
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        import agent_framework as af
        from agent_framework.openai import OpenAIResponsesClient
        from src.infrastructure.azure_tools import (
            query_resource_graph_async,
            list_nsg_rules_async,
            get_resource_details_async,
            query_activity_log_async,
            get_resource_health_async,
            list_advisor_recommendations_async,
            list_defender_assessments_async,
            list_policy_violations_async,
        )

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )
        azure_openai = AsyncAzureOpenAI(
            azure_endpoint=self._cfg.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2025-03-01-preview",  # Responses API requires >=2025-03-01-preview
            timeout=float(self._cfg.llm_timeout),
        )
        client = OpenAIResponsesClient(
            async_client=azure_openai,
            model_id=self._cfg.azure_openai_deployment,
        )

        proposals_holder: list[ProposedAction] = []
        scan_notes: list[str] = []  # captured tool-call results for scan log visibility
        _rule_findings: list = []  # populated by rules prescan for coverage manifest

        # ── Phase 40E: Universal Rules Engine pre-pass ─────────────────────────
        # Security + hygiene rules run deterministically before the LLM.
        rules_findings_text = ""
        if self._cfg.use_rules_engine and inventory is not None:
            from src.rules.base import Category
            from src.rules.agent_integration import run_rules_prescan
            rule_proposals, _rule_findings, rules_findings_text = run_rules_prescan(
                inventory, [Category.SECURITY, Category.HYGIENE], _AGENT_ID
            )
            proposals_holder.extend(rule_proposals)
            self._last_rule_findings = _rule_findings
            scan_notes.append(
                f"Rules engine: {len(_rule_findings)} security/hygiene finding(s) → "
                f"{len(rule_proposals)} proposal(s)"
            )
            if len(_rule_findings) < 1 and len(inventory) > 50:
                scan_notes.append(
                    "coverage_warning: 0 rule findings on >50-resource inventory — "
                    "check rule coverage"
                )

        # ── Pre-scan: Microsoft APIs detect first ─────────────────────────────
        # Run Advisor, Defender, and Policy BEFORE the LLM scan so the agent
        # receives confirmed findings as context — it investigates and enriches
        # rather than running a checklist from scratch.
        raw_findings: list[dict] = []

        try:
            advisor_recs = await list_advisor_recommendations_async(
                scope=target_resource_group or None, category="Security"
            )
            advisor_high = [r for r in advisor_recs if str(r.get("impact", "")).lower() == "high"]
            for rec in advisor_high:
                short_desc = (
                    rec.get("shortDescription", {}).get("problem", "")
                    if isinstance(rec.get("shortDescription"), dict)
                    else str(rec.get("shortDescription", ""))
                ) or rec.get("description", "")
                raw_findings.append({
                    "source": "ADVISOR-HIGH",
                    "severity": "HIGH",
                    "resource_name": rec.get("impactedValue", ""),
                    "description": short_desc,
                    "rec_id": rec.get("id", ""),
                    "resource_type": rec.get("impactedField", ""),
                })
            scan_notes.append(
                f"Pre-scan: Azure Advisor Security — {len(advisor_high)} HIGH recommendation(s)"
            )
        except Exception as exc:
            logger.warning("DeployAgent: pre-scan Advisor failed: %s", exc)
            scan_notes.append(f"Pre-scan: Azure Advisor unavailable ({exc})")

        try:
            defender_assessments = await list_defender_assessments_async(
                scope=target_resource_group or None
            )
            defender_high = [
                a for a in defender_assessments if str(a.get("severity", "")).lower() == "high"
            ]
            for a in defender_high:
                raw_findings.append({
                    "source": "DEFENDER-HIGH",
                    "severity": "HIGH",
                    "resource_id": a.get("resourceId", ""),
                    "resource_name": a.get("resourceName", ""),
                    "description": a.get("assessmentName", ""),
                    "remediation": a.get("remediation", ""),
                    "resource_type": "unknown",
                })
            scan_notes.append(
                f"Pre-scan: Defender for Cloud — {len(defender_high)} HIGH assessment(s)"
            )
        except Exception as exc:
            logger.warning("DeployAgent: pre-scan Defender failed: %s", exc)
            scan_notes.append(f"Pre-scan: Defender for Cloud unavailable ({exc})")

        try:
            policy_violations = await list_policy_violations_async(
                scope=target_resource_group or None
            )
            for v in policy_violations:
                assignment = v.get("policyAssignmentName", "")
                raw_findings.append({
                    "source": "POLICY-NONCOMPLIANT",
                    "severity": "MEDIUM",
                    "resource_id": v.get("resourceId", ""),
                    "resource_name": v.get("resourceName", ""),
                    "description": (
                        f"Policy: {v.get('policyDefinitionName', '')}"
                        + (f" (assignment: {assignment})" if assignment else "")
                    ),
                    "resource_type": "unknown",
                })
            scan_notes.append(
                f"Pre-scan: Azure Policy — {len(policy_violations)} non-compliant resource(s)"
            )
        except Exception as exc:
            logger.warning("DeployAgent: pre-scan Policy failed: %s", exc)
            scan_notes.append(f"Pre-scan: Azure Policy unavailable ({exc})")

        # Build findings summary to inject into the LLM prompt.
        if raw_findings:
            findings_lines = [
                f"=== PRE-COMPUTED FINDINGS: {len(raw_findings)} issue(s) from Microsoft Security APIs ===",
                "Each finding below was detected deterministically. For each one:",
                "1. Call get_resource_details to confirm the issue is current.",
                "2. Call list_nsg_rules if the resource is network-related.",
                "3. Assess blast radius and call propose_action with full evidence.",
                "",
            ]
            for i, f in enumerate(raw_findings, 1):
                r_name = f.get("resource_name") or f.get("resource_id", "?")
                line = f"[{i}] [{f.get('source', '?')}] Resource: {r_name} — {f.get('description', '?')}"
                if f.get("remediation"):
                    line += f" | Hint: {f['remediation']}"
                findings_lines.append(line)
            findings_lines.append("")
            api_findings_text = "\n".join(findings_lines)
        else:
            api_findings_text = (
                "=== PRE-COMPUTED FINDINGS: No high-severity issues detected by "
                "Microsoft Security APIs ===\n"
            )
        findings_text = (rules_findings_text + "\n" + api_findings_text) if rules_findings_text else api_findings_text

        @af.tool(
            name="query_resource_graph",
            description=(
                "Query Azure Resource Graph with a Kusto (KQL) query to discover "
                "resources. Returns JSON array with id, name, type, resourceGroup, tags."
            ),
        )
        async def tool_query_resource_graph(kusto_query: str) -> str:
            """Discover NSGs, VMs, and other resources."""
            results = await query_resource_graph_async(kusto_query)
            names = [r.get("name", "?") for r in results[:8]]
            scan_notes.append(
                f"Resource Graph query → {len(results)} resource(s) found"
                + (f": {', '.join(names)}" if names else " (none)")
            )
            return json.dumps(results, default=str)

        @af.tool(
            name="list_nsg_rules",
            description=(
                "List the security rules for an Azure Network Security Group. "
                "Returns JSON array of rules with name, port, access (Allow/Deny), "
                "priority, and direction fields."
            ),
        )
        async def tool_list_nsg_rules(nsg_resource_id: str) -> str:
            """Inspect actual NSG security rules to check for deny-all rules."""
            rules = await list_nsg_rules_async(nsg_resource_id)
            nsg_name = nsg_resource_id.split("/")[-1]

            def _props(r: dict) -> dict:
                """Flatten properties sub-dict if present (Azure live API format)."""
                p = r.get("properties", {})
                return {**r, **p} if p else r

            flat_rules = [_props(r) for r in rules]
            inbound_allow = [
                r for r in flat_rules
                if r.get("direction", "").lower() == "inbound"
                and r.get("access", "").lower() == "allow"
            ]
            open_ports = [
                str(r.get("destinationPortRange") or r.get("port") or "?")
                for r in inbound_allow[:10]
            ]
            has_deny_all = any(
                r.get("access", "").lower() == "deny"
                and r.get("destinationPortRange") in ("*", "Any")
                and r.get("direction", "").lower() == "inbound"
                for r in flat_rules
            )
            # Flag inbound Allow rules with broad source (internet-exposed)
            _broad_sources = {"*", "any", "internet", "0.0.0.0/0"}
            internet_exposed = [
                r for r in inbound_allow
                if str(r.get("sourceAddressPrefix") or r.get("properties", {}).get("sourceAddressPrefix") or "").lower()
                in _broad_sources
            ]
            _sensitive_ports = {"22", "3389", "5985", "5986", "23", "21", "*"}
            critical_rules = [
                r for r in internet_exposed
                if str(r.get("destinationPortRange") or r.get("properties", {}).get("destinationPortRange") or "")
                in _sensitive_ports
            ]

            # Show inbound-allow rules with source+port for clear LLM signal
            rule_detail = ", ".join(
                f"{r.get('name','?')}(src={r.get('sourceAddressPrefix') or r.get('properties',{}).get('sourceAddressPrefix','?')} port={r.get('destinationPortRange') or r.get('properties',{}).get('destinationPortRange','?')})"
                for r in inbound_allow[:10]
            )
            note = (
                f"NSG '{nsg_name}': {len(rules)} custom rules | inbound Allow: {len(inbound_allow)}"
                + (f" [{rule_detail}]" if rule_detail else "")
                + (f" | ⚠ INTERNET-EXPOSED rules: {len(internet_exposed)}" if internet_exposed else "")
                + (f" | 🚨 CRITICAL open management ports: {len(critical_rules)}" if critical_rules else "")
                + (" | explicit deny-all: YES" if has_deny_all else " | no explicit deny-all")
            )
            scan_notes.append(note)

            # ── Auto-propose for CRITICAL findings (deterministic path) ──────
            # The LLM must call propose_action after reading this tool result,
            # but LLM non-determinism means it can sometimes decide "no issues"
            # on re-runs (e.g. it reasons the issue is already known/flagged).
            # Internet-exposed management ports are too dangerous to leave to
            # LLM discretion — auto-propose here so they are ALWAYS captured.
            # tool_propose_action deduplicates so the LLM calling it too is safe.
            if critical_rules:
                already_proposed = any(
                    p.target.resource_id == nsg_resource_id
                    and p.action_type == ActionType.MODIFY_NSG
                    for p in proposals_holder
                )
                if not already_proposed:
                    rule_descriptions = "; ".join(
                        f"'{r.get('name','?')}' port={r.get('destinationPortRange') or r.get('properties',{}).get('destinationPortRange','?')} src={r.get('sourceAddressPrefix') or r.get('properties',{}).get('sourceAddressPrefix','?')}"
                        for r in critical_rules[:5]
                    )
                    proposals_holder.append(ProposedAction(
                        agent_id=_AGENT_ID,
                        action_type=ActionType.MODIFY_NSG,
                        target=ActionTarget(
                            resource_id=nsg_resource_id,
                            resource_type="Microsoft.Network/networkSecurityGroups",
                        ),
                        reason=(
                            f"CRITICAL: {len(critical_rules)} internet-exposed management "
                            f"port rule(s) on NSG '{nsg_name}': {rule_descriptions}. "
                            "Each rule exposes a management port to the open internet "
                            "(source=*/Any/Internet), creating unauthorized remote access "
                            "and RCE risk. Remove or restrict these rules immediately."
                        ),
                        urgency=Urgency.HIGH,
                        nsg_change_direction="restrict",
                        nsg_rule_names=[
                            r.get("name") or r.get("properties", {}).get("name", "")
                            for r in critical_rules[:5]
                            if r.get("name") or r.get("properties", {}).get("name")
                        ],
                    ))
                    logger.info(
                        "DeployAgent: auto-proposed CRITICAL NSG fix — %d rule(s) on %s",
                        len(critical_rules), nsg_name,
                    )

            # Auto-propose for missing deny-all (MEDIUM but important)
            if not has_deny_all and inbound_allow:
                already_deny = any(
                    p.target.resource_id == nsg_resource_id
                    and p.action_type == ActionType.MODIFY_NSG
                    for p in proposals_holder
                )
                if not already_deny:
                    proposals_holder.append(ProposedAction(
                        agent_id=_AGENT_ID,
                        action_type=ActionType.MODIFY_NSG,
                        target=ActionTarget(
                            resource_id=nsg_resource_id,
                            resource_type="Microsoft.Network/networkSecurityGroups",
                        ),
                        reason=(
                            f"NSG '{nsg_name}' has {len(inbound_allow)} inbound Allow rule(s) "
                            "but no explicit deny-all inbound rule. Relies on Azure's implicit "
                            "deny only — not auditable. Add a priority-4096 deny-all rule."
                        ),
                        urgency=Urgency.MEDIUM,
                        nsg_change_direction="restrict",
                    ))
                    logger.info(
                        "DeployAgent: auto-proposed deny-all fix on %s", nsg_name,
                    )

            return json.dumps(rules, default=str)

        @af.tool(
            name="get_resource_details",
            description=(
                "Get full details for an Azure resource by its ARM resource ID or "
                "short name. Returns SKU, tags, location, properties (including "
                "encryption status, authentication config, network access settings, "
                "publicNetworkAccess, enableSoftDelete, enablePurgeProtection). "
                "For VMs, also returns powerState. Use this to assess security posture."
            ),
        )
        async def tool_get_resource_details(resource_id: str) -> str:
            """Check resource tags and configuration."""
            details = await get_resource_details_async(resource_id)

            # ── Deterministic security detection ─────────────────────────────
            # The LLM interprets this JSON and should call propose_action, but
            # LLM non-determinism means it can skip findings on any run.
            # Binary security checks are too important to leave to LLM discretion.
            # Auto-propose for HIGH/CRITICAL findings; the LLM adds context.
            # tool_propose_action deduplicates, so LLM calling it too is safe.
            props = details.get("properties", {})
            res_type = (details.get("type") or "").lower()
            res_name = (details.get("name") or resource_id.split("/")[-1])
            res_rg = ""
            if "/" in resource_id:
                parts = resource_id.split("/")
                if len(parts) > 4 and parts[3].lower() == "resourcegroups":
                    res_rg = parts[4]

            def _auto_propose(reason: str, urgency: Urgency = Urgency.HIGH) -> None:
                """Auto-propose update_config if not already proposed for this resource."""
                if any(
                    p.target.resource_id == resource_id
                    and p.action_type == ActionType.UPDATE_CONFIG
                    for p in proposals_holder
                ):
                    return
                proposals_holder.append(ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.UPDATE_CONFIG,
                    target=ActionTarget(
                        resource_id=resource_id,
                        resource_type=details.get("type", "unknown"),
                        resource_group=res_rg or None,
                    ),
                    reason=reason,
                    urgency=urgency,
                ))
                logger.info("DeployAgent: auto-proposed %s on %s", urgency.value, res_name)

            # Storage account security checks
            if "storageaccounts" in res_type:
                if props.get("allowBlobPublicAccess") is True:
                    _auto_propose(
                        f"HIGH: Storage account '{res_name}' has allowBlobPublicAccess=true. "
                        "Any container can be made publicly accessible, risking data exposure. "
                        "Disable public blob access immediately."
                    )
                if props.get("supportsHttpsTrafficOnly") is False:
                    _auto_propose(
                        f"HIGH: Storage account '{res_name}' allows HTTP (unencrypted) traffic. "
                        "Data is transmitted in plaintext. Enforce HTTPS-only."
                    )

            # Key Vault security checks
            if "vaults" in res_type and "keyvault" in res_type:
                if props.get("enableSoftDelete") is False:
                    _auto_propose(
                        f"HIGH: Key Vault '{res_name}' has soft-delete disabled. "
                        "Accidental deletion of keys/secrets is permanent and unrecoverable. "
                        "Enable soft-delete immediately."
                    )

            # Database security checks (Cosmos DB, SQL)
            if any(t in res_type for t in ("databaseaccounts", "sql/servers")):
                if str(props.get("publicNetworkAccess", "")).lower() == "enabled":
                    _auto_propose(
                        f"MEDIUM: Database '{res_name}' has publicNetworkAccess=Enabled. "
                        "The database is reachable from the public internet. "
                        "Restrict to private endpoints or VNet rules.",
                        urgency=Urgency.MEDIUM,
                    )

            return json.dumps(details, default=str)

        @af.tool(
            name="query_activity_log",
            description=(
                "Query Azure Monitor activity logs for a resource group. "
                "Returns recent operations with timestamp, operation name, status "
                "(Succeeded/Failed), caller, and resource. "
                "timespan uses ISO 8601 format (e.g. 'P7D' for last 7 days)."
            ),
        )
        async def tool_query_activity_log(
            resource_group: str,
            timespan: str = "P7D",
        ) -> str:
            """Review recent changes and look for failed operations."""
            entries = await query_activity_log_async(resource_group, timespan)
            return json.dumps(entries, default=str)

        @af.tool(
            name="get_resource_health",
            description=(
                "Get Azure Resource Health status for a specific resource. "
                "Returns availabilityState (Available/Unavailable/Degraded/Unknown), "
                "a human-readable summary, reasonType, and timestamps. "
                "Use this when you need authoritative platform-level health status "
                "independent of configuration or metrics."
            ),
        )
        async def tool_get_resource_health(resource_id: str) -> str:
            """Check Azure Platform health signal for a resource."""
            health = await get_resource_health_async(resource_id)
            return json.dumps(health, default=str)

        @af.tool(
            name="list_advisor_recommendations",
            description=(
                "List Azure Advisor recommendations for the subscription or a scoped resource group. "
                "Returns recommendations with category (Cost/Security/HighAvailability/Performance), "
                "impact (High/Medium/Low), impactedValue (resource name), shortDescription, "
                "and remediation guidance. "
                "scope (optional): filter by resource group name. "
                "category (optional): filter by one category — e.g. 'Security'."
            ),
        )
        async def tool_list_advisor_recommendations(
            scope: str = "", category: str = ""
        ) -> str:
            """Retrieve pre-computed Microsoft Advisor recommendations."""
            recs = await list_advisor_recommendations_async(
                scope=scope or None, category=category or None
            )
            return json.dumps(recs, default=str)

        @af.tool(
            name="list_defender_assessments",
            description=(
                "List Microsoft Defender for Cloud security assessments (unhealthy findings). "
                "Returns per-resource security assessments from Defender's continuous evaluation "
                "against CIS, NIST, PCI-DSS, and Azure Security Benchmark. "
                "Each result includes assessmentName, severity (High/Medium/Low), resourceId, "
                "resourceName, description, and remediation guidance. "
                "scope (optional): filter by resource group name."
            ),
        )
        async def tool_list_defender_assessments(scope: str = "") -> str:
            """Retrieve Defender for Cloud unhealthy assessments."""
            assessments = await list_defender_assessments_async(
                scope=scope or None,
            )
            return json.dumps(assessments, default=str)

        @af.tool(
            name="list_policy_violations",
            description=(
                "List Azure Policy non-compliant resources. "
                "Returns resources that violate assigned compliance policies "
                "(CIS, NIST, PCI-DSS, Azure Security Benchmark, custom policies). "
                "Each result includes policyDefinitionName, resourceId, resourceName, "
                "policyAssignmentName, and category. "
                "scope (optional): filter by resource group name."
            ),
        )
        async def tool_list_policy_violations(scope: str = "") -> str:
            """Retrieve Azure Policy non-compliant resources."""
            violations = await list_policy_violations_async(
                scope=scope or None,
            )
            return json.dumps(violations, default=str)

        @af.tool(
            name="propose_action",
            description=(
                "Submit a security or configuration governance proposal. "
                "action_type must be one of: modify_nsg, update_config, create_resource, "
                "scale_up, scale_down, delete_resource, restart_service. "
                "urgency must be one of: low, medium, high. "
                "evidence_json: JSON string with observed data — include "
                "defender_assessment_id, policy_violation_id, advisor_recommendation_id "
                "in the context field. Pass {} if no structured evidence."
            ),
        )
        def tool_propose_action(
            resource_id: str,
            action_type: str,
            reason: str,
            urgency: str = "medium",
            resource_type: str = "",
            resource_group: str = "",
            evidence_json: str = "{}",
        ) -> str:
            """Validate and record a ProposedAction."""
            try:
                action_type_enum = ActionType(action_type.lower())
            except ValueError:
                valid = [e.value for e in ActionType]
                return f"ERROR: Invalid action_type '{action_type}'. Valid: {valid}"
            try:
                urgency_enum = Urgency(urgency.lower())
            except ValueError:
                urgency_enum = Urgency.MEDIUM

            # Deduplicate: CRITICAL NSG findings are auto-proposed by tool_list_nsg_rules.
            # If the LLM also calls propose_action for the same (resource_id, action_type),
            # skip it silently to avoid duplicate governance pipeline evaluations.
            if any(
                p.target.resource_id == resource_id and p.action_type == action_type_enum
                for p in proposals_holder
            ):
                name = resource_id.split("/")[-1]
                logger.debug("DeployAgent: deduped proposal — %s on %s", action_type, name)
                return f"Already proposed: {action_type} on {name} (governance dedup)"

            if not resource_group and "/" in resource_id:
                parts = resource_id.split("/")
                if len(parts) > 4 and parts[3].lower() == "resourcegroups":
                    resource_group = parts[4]
            if not resource_type and "/" in resource_id:
                parts = resource_id.split("/")
                if len(parts) > 7:
                    resource_type = f"{parts[6]}/{parts[7]}"

            # Deterministic gate: block proposals where the reason signals compliance.
            # This cannot be overridden by LLM non-determinism or instruction drift.
            if is_compliant_reason(reason):
                name = resource_id.split("/")[-1]
                logger.info(
                    "DeployAgent: blocked compliant-resource proposal — %s on %s",
                    action_type, name,
                )
                return (
                    f"Proposal rejected: reason indicates resource is already compliant "
                    f"— no governance action needed for {name}"
                )

            evidence: EvidencePayload | None = None
            try:
                ev_dict = json.loads(evidence_json) if evidence_json else {}
                if ev_dict:
                    evidence = EvidencePayload(**ev_dict)
            except Exception:
                pass  # malformed evidence JSON — drop it, don't block the proposal

            proposal = ProposedAction(
                agent_id=_AGENT_ID,
                action_type=action_type_enum,
                target=ActionTarget(
                    resource_id=resource_id,
                    resource_type=resource_type or "unknown",
                    resource_group=resource_group or None,
                ),
                reason=reason,
                urgency=urgency_enum,
                # ADR (Phase 22F): DeployAgent is remediation-only — it detects dangerous
                # NSG rules (e.g. SSH open to 0.0.0.0/0) and proposes to restrict them.
                # "restrict" is always correct here. If future scope adds port-opening proposals,
                # this logic must be updated to derive direction from the actual proposed delta.
                nsg_change_direction="restrict" if action_type_enum == ActionType.MODIFY_NSG else None,
                evidence=evidence,
            )
            proposals_holder.append(proposal)
            name = resource_id.split("/")[-1]
            logger.info("DeployAgent: proposal submitted — %s on %s", action_type, name)
            return f"Proposal submitted: {action_type} on {name}"

        agent = client.as_agent(
            name="deploy-agent",
            instructions=_AGENT_INSTRUCTIONS,
            tools=[
                tool_query_resource_graph,
                tool_list_nsg_rules,
                tool_get_resource_details,
                tool_query_activity_log,
                tool_get_resource_health,
                tool_list_advisor_recommendations,
                tool_list_defender_assessments,
                tool_list_policy_violations,
                tool_propose_action,
            ],
        )

        rg_scope = (
            f"in resource group '{target_resource_group}'"
            if target_resource_group
            else "across the Azure environment"
        )

        # Build prompt — inject pre-computed findings + inventory (if provided).
        if inventory is not None:
            from src.infrastructure.inventory_formatter import format_inventory_for_prompt  # noqa: PLC0415
            inventory_text = format_inventory_for_prompt({
                "resources": inventory,
                "resource_count": len(inventory),
                "refreshed_at": "pre-fetched",
            })
            scan_prompt = (
                f"{findings_text}\n\n"
                f"{inventory_text}\n\n"
                f"Conduct a full security and configuration compliance audit {rg_scope}. "
                "FIRST: Investigate EVERY finding listed above — confirm each issue, "
                "assess blast radius, and propose action with full evidence. "
                "THEN: Review the resource inventory for additional issues the APIs missed: "
                "NSG rules (internet-exposed ports, missing deny-all), "
                "storage security (public access, HTTPS, TLS, network ACLs), "
                "database/Key Vault config (publicNetworkAccess, soft delete, purge protection), "
                "VM security posture (disk encryption, auth type, public IP without NSG), "
                "activity log for suspicious changes (last 48h), "
                "resources with zero tags (LOW). "
                "Propose an action for EVERY finding you discover."
            )
        else:
            scan_prompt = (
                f"{findings_text}\n\n"
                f"Conduct a full security and configuration compliance audit {rg_scope}. "
                "FIRST: Investigate EVERY finding listed above — confirm each issue, "
                "assess blast radius, and propose action with full evidence. "
                "THEN: Query ALL resource types for additional issues the APIs missed — "
                "do NOT filter by type. Use the open-ended KQL from your instructions. "
                "Check: NSG rules, storage security, database/Key Vault config, "
                "VM security posture, activity logs (last 48h), and tagging gaps. "
                "Propose an action for EVERY finding you discover."
            )

        from src.infrastructure.llm_throttle import run_with_throttle
        await run_with_throttle(agent.run, scan_prompt)

        # ── Post-scan safety net: auto-propose raw_findings LLM missed ────
        # APIs ran pre-scan (no new calls here). If the LLM skipped any
        # pre-computed finding, we auto-propose it as a belt-and-suspenders.
        pre_auto_count = len(proposals_holder)
        for finding in raw_findings:
            resource_id = finding.get("resource_id", "")
            if not resource_id:
                rec_id = finding.get("rec_id", "")
                if rec_id and "/providers/" in rec_id:
                    idx = rec_id.find("/providers/Microsoft.Advisor")
                    if idx > 0:
                        resource_id = rec_id[:idx]
            if not resource_id:
                resource_id = finding.get("resource_name", "")
            if not resource_id:
                continue

            resource_name = finding.get("resource_name", "")
            already = any(
                p.target.resource_id == resource_id
                or (resource_name and resource_name in (p.target.resource_id or ""))
                for p in proposals_holder
            )
            if already:
                continue

            src = finding.get("source", "UNKNOWN")
            desc = finding.get("description", "")
            sev = finding.get("severity", "MEDIUM")
            urgency_enum = Urgency.HIGH if sev == "HIGH" else Urgency.MEDIUM
            rem = finding.get("remediation", "")
            reason = f"{src}: {desc}"
            if rem:
                reason += f". Remediation: {rem}"

            proposals_holder.append(ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.UPDATE_CONFIG,
                target=ActionTarget(
                    resource_id=resource_id,
                    resource_type=finding.get("resource_type", "unknown"),
                ),
                reason=reason,
                urgency=urgency_enum,
            ))

        auto_missed = len(proposals_holder) - pre_auto_count
        if auto_missed:
            scan_notes.append(
                f"Post-scan safety net: {auto_missed} pre-computed finding(s) "
                "auto-proposed (LLM did not cover them)"
            )

        # Phase 40E: dedup — rule-derived proposals win over LLM re-proposals.
        if self._cfg.use_rules_engine:
            from src.rules.agent_integration import dedup_proposals as _dedup_dep
            proposals_holder = _dedup_dep(proposals_holder)

        # ── Post-scan integrity log ──────────────────────────────────────
        rule_count_dep = sum(
            1 for p in proposals_holder
            if (p.reason or "").startswith(("[UNIV-", "[TYPE-"))
        )
        auto_count = sum(
            1 for p in proposals_holder
            if p.reason.startswith(("CRITICAL:", "HIGH:", "MEDIUM:", "NSG '",
                                    "ADVISOR-HIGH:", "DEFENDER-HIGH:", "POLICY-NONCOMPLIANT:"))
        )
        llm_count = len(proposals_holder) - rule_count_dep - auto_count
        scan_notes.append(
            f"rules_completed: {len(_rule_findings)} finding(s)"
        )
        if proposals_holder:
            scan_notes.append(
                f"Scan complete — {len(proposals_holder)} proposal(s): "
                f"{rule_count_dep} rules-engine, {auto_count} API, {llm_count} LLM-originated"
            )
        else:
            scan_notes.append(
                "Scan complete — no actionable issues found in Azure environment."
            )

        self.scan_notes: list[str] = scan_notes
        return proposals_holder

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Demo mode — realistic sample proposals for pipeline testing
    # ------------------------------------------------------------------

    def _demo_proposals(self) -> list[ProposedAction]:
        """Return 2 realistic sample proposals for DEMO_MODE=true."""
        return [
            ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.MODIFY_NSG,
                target=ActionTarget(
                    resource_id="nsg-demo-prod",
                    resource_type="Microsoft.Network/networkSecurityGroups",
                ),
                reason=(
                    "[DEMO] Port 22 (SSH) open to 0.0.0.0/0 on nsg-demo-prod. "
                    "No deny-all inbound rule at priority 4096. "
                    "Recommend adding deny-all rule to reduce attack surface."
                ),
                urgency=Urgency.HIGH,
                nsg_change_direction="restrict",
            ),
            ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.UPDATE_CONFIG,
                target=ActionTarget(
                    resource_id="storage-demo-01",
                    resource_type="Microsoft.Storage/storageAccounts",
                ),
                reason=(
                    "[DEMO] Storage account storage-demo-01 has no lifecycle management tags "
                    "(no backup policy, owner, or DR designation). "
                    "Adding environment and criticality metadata recommended."
                ),
                urgency=Urgency.LOW,
            ),
        ]

    # Deterministic rule-based scan (fallback / mock mode)
    # ------------------------------------------------------------------

    def _scan_rules(self) -> list[ProposedAction]:
        """Run all detection rules and aggregate deployment proposals."""
        proposals: list[ProposedAction] = []
        proposals.extend(self._detect_nsg_without_deny_all())
        proposals.extend(self._detect_missing_lifecycle_tags())
        proposals.extend(self._detect_sparse_topology())
        logger.info(
            "DeployAgent: scan complete — %d deployment proposals generated", len(proposals)
        )
        return proposals

    # ------------------------------------------------------------------
    # Detection rules (rule-based path)
    # ------------------------------------------------------------------

    def _detect_nsg_without_deny_all(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        for resource in self._resources.values():
            if resource.get("type") != _NSG_RESOURCE_TYPE:
                continue
            tags = resource.get("tags") or {}
            has_deny_all = tags.get("deny_all_inbound") == "true"
            if has_deny_all:
                continue
            reason = (
                f"NSG '{resource['name']}' does not have an explicit deny-all "
                "inbound rule documented in its tags. "
                "Best practice requires a priority-4096 deny-all rule to ensure "
                "the security posture is intentional and auditable. "
                "Propose adding the deny-all rule via NSG modification."
            )
            proposals.append(
                ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.MODIFY_NSG,
                    target=ActionTarget(
                        resource_id=resource["id"],
                        resource_type=resource["type"],
                        resource_group=resource.get("resource_group"),
                        current_monthly_cost=resource.get("monthly_cost"),
                    ),
                    reason=reason,
                    urgency=Urgency.MEDIUM,
                    nsg_change_direction="restrict",
                )
            )
            logger.info("DeployAgent: NSG without deny-all — '%s'", resource["name"])
        return proposals

    def _detect_missing_lifecycle_tags(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        for resource in self._resources.values():
            tags = resource.get("tags") or {}
            if tags.get("environment") != "production":
                continue
            # A resource is considered adequately tagged if it has ANY tag beyond
            # the classification keys (environment, criticality).  Organisation-
            # specific lifecycle tag key names are intentionally not checked here.
            has_lifecycle_tag = any(k not in _CLASSIFICATION_TAGS for k in tags)
            if has_lifecycle_tag:
                continue
            reason = (
                f"Production resource '{resource['name']}' has no lifecycle "
                "management tags. Resources tagged only with classification keys "
                "(environment, criticality) lack ownership and lifecycle context "
                "(e.g. backup policy, DR designation, cost centre, owner). "
                "Propose adding lifecycle metadata via config update."
            )
            proposals.append(
                ProposedAction(
                    agent_id=_AGENT_ID,
                    action_type=ActionType.UPDATE_CONFIG,
                    target=ActionTarget(
                        resource_id=resource["id"],
                        resource_type=resource["type"],
                        resource_group=resource.get("resource_group"),
                        current_monthly_cost=resource.get("monthly_cost"),
                    ),
                    reason=reason,
                    urgency=Urgency.LOW,
                )
            )
            logger.info("DeployAgent: missing lifecycle tags — '%s'", resource["name"])
        return proposals

    def _detect_sparse_topology(self) -> list[ProposedAction]:
        proposals: list[ProposedAction] = []
        if len(self._all_resources) >= _SPARSE_TOPOLOGY_THRESHOLD:
            return proposals
        reason = (
            f"Resource topology contains only {len(self._all_resources)} resource(s), "
            f"below the observability threshold ({_SPARSE_TOPOLOGY_THRESHOLD}). "
            "No monitoring or observability resource is likely deployed. "
            "Propose creating a Log Analytics workspace to provide infrastructure "
            "visibility and enable alert-driven governance."
        )
        proposals.append(
            ProposedAction(
                agent_id=_AGENT_ID,
                action_type=ActionType.CREATE_RESOURCE,
                target=ActionTarget(
                    resource_id="new-log-analytics-workspace",
                    resource_type="Microsoft.OperationalInsights/workspaces",
                ),
                reason=reason,
                urgency=Urgency.LOW,
            )
        )
        logger.info("DeployAgent: sparse topology — proposing observability resource")
        return proposals
